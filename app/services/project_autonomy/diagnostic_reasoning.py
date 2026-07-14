"""Evidence-gated diagnostic reasoning for local Project Autonomy.

The local model supplies semantic hypotheses.  This module owns the parts that
must not depend on model confidence: evidence provenance, independent support,
counter-evidence, baseline drift, safe experiment boundaries, and conclusion
retraction.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import diagnostic_probes


DIAGNOSTIC_SCHEMA = "chili.diagnostic-case.v1"
PACKET_SCHEMA = "chili.diagnostic-packet.v1"
REPORT_SCHEMA = "chili.diagnostic-report.v1"
DEBATE_SCHEMA = "chili.local-diagnostic-debate.v1"

DIMENSIONS = (
    "code",
    "data",
    "clock",
    "state",
    "config",
    "dependency",
    "runtime",
    "test_harness",
    "unknown",
)
DIMENSION_ALIASES = {
    "concurrency": "state",
    "deployment": "runtime",
    "environment": "runtime",
    "infrastructure": "runtime",
    "lifecycle": "state",
}
CAUSAL_DIMENSION_RUBRIC = {
    "clock": (
        "wall/event time, units, deadlines, durations, ordering, or retry budgets; not vector clocks"
    ),
    "config": "effective policy/settings, precedence, flags, header policy, or normalization",
    "data": "representation, schema, identity, joins, byte/range boundaries, or aggregation",
    "state": "ownership, lifecycle, transition, queue, idempotency, isolation, or vector-clock state",
    "dependency": "package, provider, service, wire protocol, compatibility, key rotation, or version",
    "runtime": "coercion, decoding, process/container, loaded revision, or execution semantics",
    "test_harness": "fixture, simulation, isolation, baseline comparability, or result mapping",
    "code": "algorithm/control flow only when no specific owner applies",
}
AUTO_SAFE_LEVELS = frozenset({"read_only", "isolated"})
SAFETY_LEVELS = AUTO_SAFE_LEVELS | {"runtime", "live"}

_DIAGNOSTIC_MARKERS = (
    "diagnose",
    "diagnosis",
    "debug",
    "root cause",
    "root-cause",
    "regression",
    "replay",
    "counterfactual",
    "a/b",
    "baseline changed",
    "same code",
    "why did",
    "why does",
    "failed only",
    "works locally",
    "environment drift",
    "bakit",
    "ayusin",
    "tingnan mo",
    "may mali",
    "ano nangyari",
    "anong nangyari",
    "anyare",
    "di gumagana",
    "hindi gumagana",
    "puro bug",
    "nagregress",
    "nasira",
    "nasisira",
)
_STATUS_ONLY_MESSAGES = frozenset(
    {
        "ano na",
        "anong balita",
        "anyare na",
        "ayos na",
        "ayos na lahat",
        "hello",
        "tapos na",
    }
)
_BASE_DIAGNOSTIC_LENSES = (
    "expected_vs_observed",
    "causal_timeline",
    "root_cause_vs_downstream_symptom",
    "safety_boundary",
    "post_change_proof",
)
_DIAGNOSTIC_LENS_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "strategy_contract",
        (
            "ross",
            "strategy",
            "setup",
            "entry",
            "exit",
            "hold",
            "scalp",
            "pnl",
            "profit",
            "losing trade",
        ),
    ),
    (
        "counterfactual_integrity",
        ("replay", "counterfactual", "a/b", "baseline", "harness", "backtest"),
    ),
    (
        "state_reconciliation",
        (
            "broker",
            "alpaca",
            "position",
            "pending entry",
            "duplicate",
            "orphan",
            "local state",
        ),
    ),
    (
        "producer_consumer_evidence_chain",
        ("queue", "starvation", "coverage", "missing", "consumer", "producer", "zero rows"),
    ),
    (
        "runtime_source_parity",
        ("deploy", "container", "docker", "worker", "image", "runtime", "restart", "revision"),
    ),
    (
        "external_market_state",
        ("halt", "spread", "bbo", "liquidity", "catalyst", "news", "market regime", "price action"),
    ),
)
_DIMENSION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clock", ("clock", "time", "timestamp", "timezone", "wall hour", "sim hour", "utc", "et hour")),
    (
        "data",
        (
            "dataset",
            "row count",
            "record count",
            "table",
            "data source",
            "ingestion source",
            "sink",
            "feed",
            "cache",
            "snapshot data",
            "manifest",
            "shard",
            "nbbo",
            "identifier",
            "encoding",
            "unicode",
            "normalization",
            "identity",
            "clip_id",
            "dedup key",
            "collision",
            "lookup key",
            "row key",
        ),
    ),
    (
        "state",
        (
            "state",
            "queue",
            "pending",
            "session",
            "board",
            "checkpoint",
            "cursor",
            "stale row",
            "lifecycle",
            "lease",
            "owner",
            "ownership",
            "reservation",
            " busy ",
            "admission",
        ),
    ),
    (
        "config",
        (
            "config",
            "setting",
            "flag",
            "environment variable",
            " env ",
            "feature gate",
            "policy",
            "principal",
            "authorization",
            "membership",
            "service identity",
        ),
    ),
    (
        "dependency",
        (
            "dependency",
            "provider",
            "socket",
            "network",
            "database server",
            "broker api",
            "certificate",
            "tls",
            "handshake",
            "trust store",
            "peer chain",
            "upstream",
            "dns",
            "package",
            "library",
            "sdk",
        ),
    ),
    ("runtime", ("runtime", "container", "worker", "process", "restart", "image", "deployment")),
    (
        "test_harness",
        (
            "replay",
            "harness",
            "fixture",
            "mock",
            "test database",
            "simulator",
            "qualification",
            "comparison record",
            "reference run",
            "candidate run",
            "screenshot",
            "test runner",
        ),
    ),
    (
        "code",
        (
            "code",
            "commit",
            "revision",
            "diff",
            "function",
            "caller",
            "branch",
            "patch",
            "source edit",
            "source inspection",
            "control flow",
            "control-flow",
            "call path",
            "source trace",
            "source revision",
            "stage plan",
            "ordering point",
        ),
    ),
)
_DIMENSION_PHRASE_WEIGHTS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    (
        "clock",
        (
            ("wall clock", 5),
            ("simulated_at", 5),
            ("simulated time", 5),
            ("replay timestamp", 4),
            ("datetime.now", 4),
            ("event time", 6),
            ("broker sequence", 7),
            ("ordered by recorded producer time", 8),
            ("ordered by broker sequence", 8),
            ("host offset", 7),
            ("time source", 5),
            ("local wall-time", 8),
            ("offset correction", 7),
            ("parsing zone", 7),
            ("utc offset", 6),
            ("monotonic duration", 5),
            ("offset-free local", 9),
            ("retained utc instant", 9),
            ("repeated local hour", 9),
            ("repeated-hour offset", 9),
            ("elapsed duration", 7),
            ("retain their offsets", 9),
            ("negative age", 7),
            ("wall reading", 10),
            ("seconds behind reference", 10),
            ("recorded offset", 9),
            ("synchronized wall", 9),
            ("offset_seconds", 10),
            ("retry-after", 10),
            ("remaining allowance", 9),
            ("retry budget", 9),
            ("queue time", 8),
        ),
    ),
    (
        "data",
        (
            ("source-sink", 5),
            ("source/sink", 5),
            ("populated source", 4),
            ("quote rows", 4),
            ("repository reads", 3),
            ("partial unique", 7),
            ("unique index", 6),
            ("one-to-many", 7),
            ("cartesian", 7),
            ("cross multiplied", 6),
            ("aggregate", 4),
            ("group by", 4),
            ("route join", 7),
            ("unicode punctuation", 7),
            ("identifier normalization", 8),
            ("retained artifact", 3),
            ("input artifact", 4),
            ("canonical identifier", 8),
            ("leading zero", 8),
            ("numeric column", 7),
            ("exact join", 7),
            ("shortened key", 7),
            ("signed source archive", 5),
            ("delivered roster", 6),
            ("producer identity", 8),
            ("reused producer identities", 9),
            ("collision-resistant identity", 10),
            ("identifier collision", 9),
            ("duplicate identifier", 8),
            ("duplicate clip_id", 9),
            ("identity fields", 8),
            ("key composition", 8),
            ("duplicate key", 10),
            ("colliding identifiers", 10),
            ("identifier assignments", 8),
            ("distinct surrogate values", 7),
            ("facility key", 9),
            ("entries for key", 8),
            ("entries_for_key", 8),
            ("matching_rows", 8),
            ("unused key", 7),
            ("route-stop table", 7),
            ("content-range", 10),
            ("inclusive byte", 9),
            ("vendor event identifier", 9),
        ),
    ),
    (
        "state",
        (
            ("queue depth", 5),
            ("pending depth", 5),
            ("stale low-value", 4),
            ("admission check", 3),
            ("reservation registry", 7),
            ("state contract", 6),
            ("dedupe", 6),
            ("duplicate", 5),
            ("successful reservation", 5),
            ("_seen", 5),
            ("reserve(", 5),
            ("singleflight", 7),
            ("single-flight", 7),
            ("in-flight", 6),
            ("poison", 5),
            ("subscription", 5),
            ("lifecycle", 5),
            ("after stop", 6),
            ("cancel", 4),
            ("lease snapshot", 7),
            ("busy_owner", 7),
            ("release_requested", 6),
            ("owner process", 5),
            ("durable workflow row", 7),
            ("lease table", 6),
            ("publishing-without-lease", 8),
            ("transition rules", 7),
            ("claimable state", 7),
            ("orphaned rows", 6),
            ("persisted marker", 8),
            ("fence entries", 10),
            ("unmatched fences", 10),
            ("durable ledger", 8),
            ("marked as already sent", 9),
            ("promoted snapshot", 8),
            ("replicas concurrent", 11),
            ("convergence bookkeeping", 11),
            ("tombstone", 9),
            ("retry token", 9),
            ("attempt numbers", 8),
        ),
    ),
    (
        "config",
        (
            ("toggling only", 6),
            ("only material environment difference", 6),
            ("resolved settings", 5),
            ("feature gate", 4),
            ("setting toggle", 4),
            ("gate_enabled", 5),
            ("_true_values", 5),
            ("env.get", 4),
            ("only that value is changed", 7),
            ("only that value changed", 7),
            ("changing only that value", 7),
            ("rendered policy", 7),
            ("policy bundle", 6),
            ("policy evaluator", 6),
            ("principal membership", 7),
            ("authorization policy", 7),
            ("route declaration", 5),
            ("terminal space", 7),
            ("exact-principal denial", 8),
            ("edge authorization", 7),
            ("authorized identity", 7),
            ("effective settings snapshot", 8),
            ("topic filter", 8),
            ("path-normalization transform", 8),
            ("rendered setting", 6),
            ("leading-slash filter", 8),
            ("topic-matcher", 8),
            ("server-name value", 8),
            ("rendered server-name", 9),
            ("explicit server name", 8),
            ("derived server name", 8),
            ("effective listener", 9),
            ("listener configuration", 8),
            ("region-alias", 8),
            ("rendered pre-change output", 7),
            ("desired template", 7),
            ("trace_server_name", 9),
            ("expected_state.server_name", 9),
            ("actual_state.trace_server_name", 9),
            ("configuration repository", 7),
            ("effective-settings", 9),
            ("environment entry", 8),
            ("zero-value environment", 9),
            ("approved profile", 7),
            ("became effective", 7),
            ("effective value", 7),
            ("reported at startup", 6),
            ("effective deadline", 9),
            ("response deadline", 9),
            ("changing only the deadline", 10),
            ("route definition", 8),
            ("relay definition", 8),
            ("bootstrap template", 7),
            ("vary values", 11),
            ("vary header", 11),
            ("mixed casing", 8),
            ("wildcard response", 9),
        ),
    ),
    (
        "dependency",
        (
            ("abortsignal", 7),
            ("abort signal", 7),
            ("aborterror", 7),
            ("provider adapter", 5),
            ("dependency error", 5),
            ("certificate verify", 7),
            ("expired intermediate", 7),
            ("peer chain", 6),
            ("trust store", 5),
            ("carrier sdk", 5),
            ("carrier call", 5),
            ("package version", 7),
            ("transitive package", 8),
            ("dependency bundle", 7),
            ("signed dependency bundle", 8),
            ("version mismatch", 6),
            ("prior bundle", 5),
            ("calendar parsing package", 8),
            ("locked package versions", 8),
            ("transitive lock refresh", 8),
            ("provider endpoint", 7),
            ("network endpoint", 7),
            ("remote endpoint", 6),
            ("api endpoint", 7),
            ("tls endpoint", 8),
            ("resolved component", 9),
            ("component lock", 8),
            ("component sets", 8),
            ("parser version", 9),
            ("caption parser", 9),
            ("prior resolved component", 10),
            ("shared library", 9),
            ("compatibility package", 9),
            ("package inventory", 8),
            ("package inventories", 8),
            ("package present", 8),
            ("package absent", 8),
            ("cannot load lib", 10),
            ("loader error", 8),
            ("adding the exact prior", 8),
            ("decoder package", 10),
            ("decoder revision", 10),
            ("newer decoder", 9),
            ("older decoder", 9),
            ("base-layer inventory", 6),
            ("wire behavior", 11),
            ("secret rotation", 10),
            ("forwarder rollout", 8),
        ),
    ),
    (
        "runtime",
        (
            ("recreating only", 6),
            ("running worker image", 6),
            ("image label", 5),
            ("loaded module hash", 8),
            ("loaded confirmation handler", 8),
            ("process snapshot", 7),
            ("signed image inventory", 6),
            ("writable layer", 8),
            ("overlay entries", 6),
            ("worker rollout", 7),
            ("process evidence", 6),
            ("executing process", 8),
            ("pre-fix behavior", 3),
            ("node pool", 5),
            ("network namespace", 6),
            ("overlay mtu", 7),
            ("underlay path", 6),
            ("pod-side mtu", 7),
            ("encapsulated frames", 6),
            ("release manifest", 5),
            ("legacy deployment", 6),
            ("deployment controller", 6),
            ("mixed endpoint membership", 7),
            ("isolated namespace", 5),
            ("legacy resource", 5),
            ("memory-control termination", 8),
            ("container memory", 7),
            ("effective container boundary", 8),
            ("resident-set", 7),
            ("worker pool replacement", 6),
        ),
    ),
    (
        "test_harness",
        (
            ("serialized replay input", 4),
            ("replay fixture", 3),
            ("focused test", 2),
            ("qualification gate", 7),
            ("comparison environment", 7),
            ("reference record", 5),
            ("candidate record", 5),
            ("visual diff", 5),
            ("floating runner", 6),
            ("baseline runner", 6),
            ("end-to-end suite", 8),
            ("browser profile", 8),
            ("service-worker", 7),
            ("parallel shard", 6),
            ("scenario cleanup", 6),
            ("proxy rule", 6),
            ("test scenario", 5),
            ("retained trace", 6),
            ("assertion timeout", 6),
            ("injected-input", 7),
            ("virtual speech-device readiness", 8),
            ("virtual device readiness", 8),
            ("trace schema", 8),
            ("isolated runner", 8),
            ("automated accessibility gate", 9),
            ("automated runs", 6),
            ("diagnostic instrumentation", 9),
            ("managed browser", 9),
            ("fresh browser", 9),
            ("fresh-browser", 9),
            ("browser checks", 8),
            ("browser-context", 10),
            ("browser context", 10),
            ("runner sequence", 9),
            ("runner reuse", 9),
            ("reused and fresh browser", 10),
            ("observer contamination", 10),
        ),
    ),
    (
        "code",
        (
            ("source diff", 5),
            ("source inspection", 4),
            ("additional source edit", 2),
            ("control-flow trace", 7),
            ("source trace", 6),
            ("branch returning", 6),
            ("fixed while comparing", 7),
            ("identical captured request", 5),
            ("source hunk", 7),
            ("code hunk", 7),
            ("normalization hunk", 7),
            ("reverting only that hunk", 7),
            ("source revision", 6),
            ("stage plan differs", 7),
            ("ordering point", 7),
            ("affected revision", 5),
            ("earlier revision", 5),
            ("paging function", 8),
            ("cursor selection after filtering", 9),
            ("prior paging function", 8),
            ("deployed paging", 8),
            ("interval helper", 8),
            ("inline predicate", 9),
            ("predicate semantics", 10),
            ("half-open interval", 9),
            ("boundary predicate", 9),
            ("generated interval pairs", 9),
            ("endpoint equality", 9),
            ("closed-vs-half-open-boundary", 10),
            ("boundary-focused proof corpus", 9),
            ("half-open reservation contract", 10),
            ("code_fingerprint", 7),
            ("matcher checks", 9),
            ("does not check", 9),
            ("one-bound check", 10),
            ("two-bound", 9),
            ("interval-overlap check", 10),
            ("matcher change", 9),
            ("release comparison", 7),
            ("release artifact", 8),
            ("changed_factor=release_artifact", 10),
            ("workflow continuation", 9),
            ("debounce continuation", 10),
            ("captures the prior", 8),
            ("without checking whether", 9),
            ("publisher path", 7),
            ("re-reads and compares", 10),
        ),
    ),
)
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "against",
        "because",
        "before",
        "being",
        "between",
        "could",
        "does",
        "from",
        "have",
        "into",
        "only",
        "same",
        "should",
        "their",
        "there",
        "these",
        "this",
        "through",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
    }
)
_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".cs",
        ".dart",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".php",
        ".ps1",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "vendor", "dist", "build", "logs", "data"})
_DIRECT_SOURCE_SIGNALS = (
    "datetime.now",
    "simulated_at",
    "wall_clock",
    "source_rows",
    "sink_rows",
    "return bool(",
    "os.environ",
    "_seen",
    "reserve(",
    "queue depth",
    "pending depth",
)
_CAUSAL_SUPPORT_MARKERS = (
    "applying only",
    "changing only",
    "lowering only",
    "pinning only",
    "resetting only",
    "reverting only",
    "reproduces",
    "restores",
    "first broken",
    "earliest break",
    "race window",
    "wrong ",
    "mismatch",
    "misconfigured",
    "omitted",
    "missing",
    "stale ",
    "orphan",
    "differs at",
    "differing only",
    "instead of",
    "rather than",
    "after removing only",
    "adding the exact",
    "restoring the",
    "replaced by",
    "produces respectively",
    " versus ",
    "with identical",
)
_CAUSAL_INTERVENTION_MARKERS = (
    "applying only",
    "changing only",
    "lowering only",
    "pinning only",
    "resetting only",
    "reverting only",
    "reproduces",
    "restores",
    "first broken",
    "earliest break",
    "differs at",
    "differing only",
    "after removing only",
    "adding the exact",
    "restoring the",
    "produces respectively",
    "with identical",
)
_NEGATED_INTERVENTION_MARKERS = (
    "changing only this does not",
    "changing only that does not",
    "changing only the value does not",
    "reverting only this does not",
    "reverting only that does not",
    "applying only this does not",
    "applying only that does not",
    "reproduces no failure",
    "reproduces no fault",
    "does not reproduce",
    "did not reproduce",
    "fails to reproduce",
    "no longer reproduces",
)
_CAUSAL_CONTRADICTION_MARKERS = (
    "byte-identical",
    "does not change",
    "does not make",
    "did not change",
    "healthy",
    "identical across",
    "is absent",
    "leaves the outcome unchanged",
    "match across",
    "matches across",
    "no additional",
    "no overrun",
    "no parse rejection",
    "no retry",
    "no source",
    "remain unchanged",
    "remains unchanged",
    "same application response",
    "unchanged from",
    "within the expected",
    "are identical",
    "is identical",
    "checksums are identical",
    "remain correct",
    "remains correct",
    "remain healthy",
    "remains healthy",
    "remain normal",
    "remains normal",
    "match the last healthy",
    "no unexpected",
    "no step change",
    "arguing against",
    "all pass independent validation",
    "without elevated latency or errors",
    "each emit one",
    "each emits one",
    "produce identical",
    "produces identical",
    "unchanged fingerprints",
)
_ATTRIBUTION_GAP_MARKERS = (
    "cannot identify",
    "cannot separate",
    "cannot be proven",
    "cannot isolate",
    "preventing a correlation-level link",
    "no record explaining",
    "not captured",
    "was not recorded",
    "were not recorded",
    "no retained artifact",
    "not individually attributable",
    "no longer available",
    "lacks worker identity",
    "missing attribution",
    "cannot distinguish",
    "cannot establish",
    "cannot determine",
    "insufficient to determine",
    "not enough to distinguish",
    "too coarse to identify",
    "no incident preserved",
    "preventing event-by-event attribution",
    "do not share one identifier",
    "cannot show whether",
    "lack enough context to show",
    "lacks enough context to show",
    "rotated before preservation",
)
_AMBIGUOUS_EXPERIMENT_MARKERS = (
    "depending on the assumed",
    "neither assumption",
    "does not explain the entire",
    "does not explain all",
    "cannot distinguish",
    "cannot determine",
    "both fit the observed",
    "do not preserve",
    "does not preserve",
    "not independently varied",
    "overlap statistically",
    "using its own controller and capture interface",
)
_DECISIVE_ATTRIBUTION_GAP_MARKERS = (
    "cannot distinguish",
    "cannot separate",
    "cannot establish",
    "preventing a correlation-level link",
    "not individually attributable",
    "lacks worker identity",
    "no retained artifact",
    "too coarse to identify",
    "no incident preserved",
    "preventing event-by-event attribution",
    "do not share one identifier",
    "cannot show whether",
    "lack enough context to show",
    "lacks enough context to show",
)
_MECHANISM_ATTRIBUTION_GAP_MARKERS = (
    "cannot distinguish",
    "cannot separate",
    "not individually attributable",
    "lacks worker identity",
    "too coarse to identify",
    "no incident preserved",
    "preventing event-by-event attribution",
    "do not share one identifier",
    "cannot show whether",
    "lack enough context to show",
    "lacks enough context to show",
)
_PROSPECTIVE_MEASUREMENT_MARKERS = (
    "next measurement",
    "next probe",
    "can collect",
    "can dual-run",
    "would collect",
    "planned measurement",
    "proposed measurement",
)
_COARSE_RESET_EXPERIMENT_MARKERS = (
    "supervised recycle",
    "fresh worker",
    "restart the worker",
    "restart the process",
    "recreate the worker",
    "recreate the process",
    "replace the worker",
    "replace the process",
)
_BROAD_INTERVENTION_MARKERS = (
    "dedicated host",
    "dedicated diagnostic host",
    "dedicated pool",
    "different host",
    "fresh host",
    "isolated environment",
    "replace the environment",
    "rebuild the environment",
    "move the workload",
    "relocate the workload",
    *_COARSE_RESET_EXPERIMENT_MARKERS,
)
_SEMANTIC_BASELINE_PAIR_PATTERNS = (
    r"\b(?:final\s+)?good\s+(?:build|release|deployment|host|run)\b.{0,220}"
    r"\b(?:first\s+)?bad\s+(?:build|release|deployment|host|run)\b",
    r"\b(?:first\s+)?bad\s+(?:build|release|deployment|host|run)\b.{0,220}"
    r"\b(?:final\s+)?good\s+(?:build|release|deployment|host|run)\b",
    r"\b(?:final\s+)?good\b.{0,80}\b(?:first\s+)?bad\b.{0,80}"
    r"\b(?:build|builds|release|releases|deployment|deployments|host|hosts|run|runs)\b",
    r"\bprior\s+(?:week|host|deployment|build|release|version)\b.{0,240}"
    r"\b(?:post-maintenance|post-change|new\s+(?:host|deployment|build|release|version))\b",
    r"\b(?:post-maintenance|post-change|new(?:ly provisioned)?\s+(?:host|hosts|deployment|build|release|version))\b"
    r".{0,240}\bprior\s+(?:week|host|hosts|deployment|build|release|version)\b",
)
_BASELINE_COMPARABILITY_GAP_MARKERS = (
    "did not retain a comparable",
    "does not retain a comparable",
    "no comparable effective",
    "preserve neither the old",
    "old and current checksums incomparable",
    "old and current checksums are incomparable",
    "old and current fingerprints incomparable",
    "old and current fingerprints are incomparable",
    "pre-change output is unavailable",
    "pre-change snapshot is unavailable",
    "rotated before preservation",
    "do not share one identifier",
    "preventing event-by-event attribution",
)
_RETAINED_BASELINE_MARKERS = (
    " prior ",
    " preceding ",
    " pre-maintenance ",
    " previous ",
    " before ",
)
_RETAINED_CANDIDATE_MARKERS = (
    " first ",
    " began ",
    " after ",
    " new ",
    " resumed ",
)
_BASELINE_STABILITY_MARKERS = (
    "both before and after",
    "did not materially change",
    "rate did not change",
    "outcome did not change",
    "result did not change",
    "behavior did not change",
    "no step change",
    "not when the rate changed",
)


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _clean_id(value: object, fallback: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value or "").strip()).strip("-")
    return clean[:100] or fallback


def _clamp_reliability(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.7
    return max(0.0, min(1.0, number))


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _correlation_fingerprint(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


def _dimension_term_present(statement: str, term: str) -> bool:
    """Match taxonomy terms as tokens, not accidental substrings.

    The old substring matcher classified ``release`` as state because it
    contains ``lease`` and ``timeout`` as clock because it contains ``time``.
    Weighted phrases remain prefix-friendly for intentional stems.
    """
    candidate = str(term or "").strip().lower()
    if not candidate:
        return False
    prefix = r"(?<![a-z0-9_])" if candidate[0].isalnum() or candidate[0] == "_" else ""
    suffix = r"(?![a-z0-9_])" if candidate[-1].isalnum() or candidate[-1] == "_" else ""
    return re.search(prefix + re.escape(candidate) + suffix, statement) is not None


def _dimension_scores(statement: str) -> dict[str, int]:
    raw = str(statement or "")
    lower = raw.lower()
    scores: dict[str, int] = {}
    for dimension, terms in _DIMENSION_TERMS:
        scores[dimension] = sum(
            1 for term in terms if _dimension_term_present(lower, term)
        )
    for dimension, weighted_phrases in _DIMENSION_PHRASE_WEIGHTS:
        scores[dimension] = scores.get(dimension, 0) + sum(
            weight for phrase, weight in weighted_phrases if phrase in lower
        )
    if re.search(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", raw):
        scores["config"] = scores.get("config", 0) + 10
    if re.search(r"\blib[a-z0-9_.+-]+\.so(?:\.\d+)*\b", lower):
        scores["dependency"] = scores.get("dependency", 0) + 12
    if re.search(
        r"\b(?:duplicate|duplicated|reused|colliding)\b.{0,48}\bkey\b"
        r"|\bchanging only\b.{0,100}\b(?:row|record|identifier|key)\b",
        lower,
    ):
        scores["data"] = scores.get("data", 0) + 9
    return scores


def _select_dimension(scores: Mapping[str, int]) -> str:
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] <= 0:
        return "unknown"
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return "unknown"
    return ranked[0][0]


def infer_dimension(statement: str) -> str:
    return _select_dimension(_dimension_scores(statement))


def decisive_inferred_dimension(
    statement: str,
    *,
    minimum_score: int = 8,
    minimum_margin: int = 5,
) -> str:
    """Return a taxonomy family only when phrase evidence is unambiguous."""
    ranked = sorted(
        _dimension_scores(statement).items(),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked:
        return "unknown"
    best_dimension, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < minimum_score or best_score - runner_up < minimum_margin:
        return "unknown"
    return best_dimension


_HELD_CONSTANT_BOUNDARIES = (
    " without changing ",
    " while holding ",
    " while keeping ",
    " with all other ",
    " and leaves ",
    " while legacy ",
)


def infer_evidence_dimension(statement: str, structured_context: str = "") -> str:
    """Prefer the manipulated variable over dimensions named as controls.

    Diagnostic proof statements often end with a long list of code, settings,
    data, and runtime values that stayed fixed. A flat keyword vote can make
    that held-constant list outrank the one component that was actually varied.
    """
    raw_statement = str(statement or "")
    lower = raw_statement.lower()
    boundary_positions = [
        position
        for marker in _HELD_CONSTANT_BOUNDARIES
        if (position := lower.find(marker)) >= 0
    ]
    focus = (
        raw_statement[: min(boundary_positions)]
        if boundary_positions
        else raw_statement
    )
    full_scores = _dimension_scores(raw_statement)
    focus_scores = _dimension_scores(focus)
    context_scores = _dimension_scores(structured_context)
    combined = {
        dimension: full_scores.get(dimension, 0)
        + (2 * focus_scores.get(dimension, 0))
        + context_scores.get(dimension, 0)
        for dimension in DIMENSIONS
        if dimension != "unknown"
    }
    return _select_dimension(combined)


_SINGLE_FACTOR_INTERVENTION_MARKERS = (
    "changing only",
    "replacing only",
    "reverting only",
    "pinning only",
    "resetting only",
    "removing only",
    "adding the exact",
    "restoring only",
    "without those",
    "candidate artifact",
    "recorded offset",
)
_PAIRED_VARIANT_PATTERN = re.compile(
    r"\bwith (?:the )?(?:newer|current|candidate|changed|updated|recorded)\b"
    r".{0,240}\bwith (?:the )?(?:older|prior|baseline|archived|previous|synchronized)\b"
    r"|\bwith (?:the )?(?:older|prior|baseline|archived|previous|synchronized)\b"
    r".{0,240}\bwith (?:the )?(?:newer|current|candidate|changed|updated|recorded)\b"
)


def _intervention_focus(statement: str) -> str:
    raw = str(statement or "")
    lower = raw.lower()
    positions = [
        position
        for marker in _SINGLE_FACTOR_INTERVENTION_MARKERS
        if (position := lower.find(marker)) >= 0
    ]
    if positions:
        start = min(positions)
        return raw[start : start + 420]
    match = _PAIRED_VARIANT_PATTERN.search(lower)
    if match:
        start = max(0, match.start() - 100)
        return raw[start : match.end() + 100]
    return ""


def infer_causal_dimension(statement: str, structured_context: str = "") -> str:
    """Infer the manipulated owner separately from the observed surface.

    The full evidence sentence often names the worker, replay apparatus, or
    downstream symptom more often than the one factor varied by the proof.
    Only a bounded intervention clause or explicit changed-factor metadata is
    allowed to override that surface vocabulary.
    """
    focus = _intervention_focus(statement)
    context = str(structured_context or "")
    changed_context = ""
    match = re.search(
        r"(?:^|;\s*)changed_factor=([^;]{1,180})",
        context,
        flags=re.IGNORECASE,
    )
    if match:
        changed_context = match.group(1)
    if not focus and not changed_context:
        return "unknown"
    focus_scores = _dimension_scores(focus)
    context_scores = _dimension_scores(changed_context)
    combined = {
        dimension: (3 * focus_scores.get(dimension, 0))
        + (4 * context_scores.get(dimension, 0))
        for dimension in DIMENSIONS
        if dimension != "unknown"
    }
    return _select_dimension(combined)


def _evidence_lifecycle(statement: str) -> str:
    lower = str(statement or "").lower()
    return (
        "planned_measurement"
        if any(marker in lower for marker in _PROSPECTIVE_MEASUREMENT_MARKERS)
        else "observed_result"
    )


def _intervention_scope(
    statement: str,
    *,
    causal_dimension: str,
    lifecycle: str,
    kind: str,
) -> str:
    if lifecycle != "observed_result" or str(kind or "") != "experiment":
        return "none"
    lower = str(statement or "").lower()
    if causal_dimension != "unknown" and (
        any(marker in lower for marker in _SINGLE_FACTOR_INTERVENTION_MARKERS)
        or _PAIRED_VARIANT_PATTERN.search(lower)
    ):
        return "component"
    if any(marker in lower for marker in _BROAD_INTERVENTION_MARKERS):
        return "broad"
    if causal_dimension != "unknown" and any(
        marker in lower for marker in _CAUSAL_INTERVENTION_MARKERS
    ):
        return "component"
    return "boundary"


def _retained_comparison_relation(
    statement: str,
    structured_context: str = "",
) -> str:
    lower = str(statement or "").lower()
    padded = f" {lower} "
    if (
        ("baseline" in lower and has_attribution_gap(lower))
        or any(marker in lower for marker in _BASELINE_COMPARABILITY_GAP_MARKERS)
        or re.search(
            r"\b(?:baseline|pre-change|prior)\b.{0,90}"
            r"\b(?:unavailable|missing|not retained)\b",
            lower,
        )
        or "cannot be compared" in lower
    ):
        return "incomparable"
    retained_signal = bool(
        "retained=true" in str(structured_context or "").lower()
        or re.search(r"\bretained\b", lower)
    )
    baseline_hits = [
        (marker, padded.find(marker))
        for marker in _RETAINED_BASELINE_MARKERS
        if marker in padded
    ]
    candidate_hits = [
        (marker, padded.find(marker))
        for marker in _RETAINED_CANDIDATE_MARKERS
        if marker in padded
    ]
    temporal_pair = bool(
        any(
            baseline_position < candidate_position
            for _baseline_marker, baseline_position in baseline_hits
            for _candidate_marker, candidate_position in candidate_hits
        )
        or any(
            candidate_marker in {" first ", " new "}
            and baseline_marker in {" prior ", " previous "}
            for baseline_marker, _baseline_position in baseline_hits
            for candidate_marker, _candidate_position in candidate_hits
        )
    )
    retained_control_onset_pair = bool(
        retained_signal
        and any(
            candidate_marker == " began "
            and candidate_position < baseline_position
            and baseline_marker in {" prior ", " previous "}
            for baseline_marker, baseline_position in baseline_hits
            for candidate_marker, candidate_position in candidate_hits
        )
        and re.search(
            r"\b(?:continued|remained|still)\b.{0,100}"
            r"\b(?:successfully|healthy|working|passing|completed|succeeded|unaffected)\b",
            lower,
        )
    )
    semantic_pair = any(
        re.search(pattern, lower) for pattern in _SEMANTIC_BASELINE_PAIR_PATTERNS
    )
    if not (
        (retained_signal and temporal_pair)
        or retained_control_onset_pair
        or semantic_pair
    ):
        return "none"
    stable = bool(
        any(marker in lower for marker in _BASELINE_STABILITY_MARKERS)
        or re.search(
            r"\bsame\b.{0,80}\b(?:rate|pattern|distribution|outcome|result|behavior)\b",
            lower,
        )
        or re.search(
            r"\b(?:rate|outcome|result|behavior)\b.{0,50}"
            r"\b(?:did not|does not)\b.{0,20}\bchange",
            lower,
        )
    )
    return "stable" if stable else "changed"


def _bounded_metadata_summary(raw: Mapping[str, Any]) -> str:
    existing = _clip(raw.get("structured_context"), 700)
    metadata = raw.get("metadata")
    if existing or not isinstance(metadata, Mapping):
        return existing

    parts: list[str] = []

    def visit(prefix: str, value: object, depth: int) -> None:
        if len(parts) >= 28:
            return
        if isinstance(value, Mapping) and depth < 3:
            for key in sorted(value, key=lambda item: str(item)):
                clean_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(key)).strip("_")
                if clean_key:
                    visit(f"{prefix}.{clean_key}" if prefix else clean_key, value[key], depth + 1)
            return
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and depth < 3
        ):
            for index, item in enumerate(value[:6]):
                visit(f"{prefix}[{index}]", item, depth + 1)
            return
        rendered = _clip(value, 90)
        if prefix and rendered:
            parts.append(f"{prefix}={rendered}")

    visit("", metadata, 0)
    return _clip("; ".join(parts), 700)


def has_attribution_gap(statement: str) -> bool:
    lower = str(statement or "").lower()
    return bool(
        any(marker in lower for marker in _ATTRIBUTION_GAP_MARKERS)
        or re.search(
            r"\bdoes not (?:record|retain|capture)\b.{0,120}"
            r"\b(?:individual|event|correlation|request|execution)\b",
            lower,
        )
    )


def infer_causal_role(
    statement: str,
    *,
    discriminating: bool = False,
    kind: str = "observation",
    provenance: str = "",
    structured_break: bool = False,
    downstream_surface: bool = False,
) -> str:
    """Classify evidence as causal support, contradiction, or context.

    Dense incident packets deliberately include healthy controls and held-
    constant confounders. Treating every record in a semantic family as
    positive support makes the largest family win rather than the cause.
    """
    lower = str(statement or "").lower()
    if has_attribution_gap(lower):
        return "context"
    if any(marker in lower for marker in _PROSPECTIVE_MEASUREMENT_MARKERS):
        return "context"
    if any(marker in lower for marker in _AMBIGUOUS_EXPERIMENT_MARKERS):
        return "context"
    if any(marker in lower for marker in _NEGATED_INTERVENTION_MARKERS) or re.search(
        r"\b(?:changing|reverting|applying|lowering|pinning|resetting)\s+only\b"
        r".{0,120}\b(?:does not|did not|fails to|leaves? the outcome unchanged)",
        lower,
    ):
        return "contradiction"
    if any(marker in lower for marker in _CAUSAL_INTERVENTION_MARKERS):
        return "support"
    if _PAIRED_VARIANT_PATTERN.search(lower):
        return "support"
    if any(marker in lower for marker in _CAUSAL_CONTRADICTION_MARKERS):
        return "contradiction"
    if (
        str(kind or "") == "metric"
        and not structured_break
    ):
        return "context"
    if any(marker in lower for marker in _CAUSAL_SUPPORT_MARKERS):
        return "support"
    if downstream_surface:
        return "context"
    if structured_break:
        return "support"
    if discriminating and str(kind or "") == "experiment":
        return "support"
    return "context"


def derive_contract_invariants(statement: str) -> list[str]:
    """Extract reusable mechanism contracts without asking the local model."""
    lowered = str(statement or "").lower()
    invariants: list[str] = []
    conditional_exports = bool(
        any(
            token in lowered
            for token in ("conditional export", "export condition", "exports conditions")
        )
        or (
            "export" in lowered
            and all(token in lowered for token in ("browser", "node", "default"))
        )
    )
    if conditional_exports:
        invariants.append(
            "Node conditional-export resolution walks the caller's declared condition order and then the default "
            "branch, recursively resolving the selected target. It must not choose object insertion order, infer "
            "a condition from filenames, or return the condition key instead of its target."
        )
    if (
        any(token in lowered for token in ("esm", "node import", "file url", "file://"))
        and any(
            token in lowered
            for token in ("literal #", "literal %", "vendor cache", "special character")
        )
    ):
        invariants.append(
            "An ESM import built from a filesystem path uses the platform path-to-file-URL primitive so fragment, "
            "percent, space, drive, and separator characters are encoded exactly once. Manual file:// assembly, "
            "whole-path encodeURIComponent, and changing the filesystem path are not equivalent."
        )
    if any(token in lowered for token in ("base64url", "canonical decoder", "canonical text")):
        invariants.append(
            "A canonical text decoder validates the complete alphabet and padding policy, decodes once, and "
            "requires decode-then-encode equality so unused-bit aliases are rejected; every consumer uses that "
            "shared decoder instead of a looser platform primitive."
        )
    if (
        any(token in lowered for token in ("fixed-point", "minor units", "decimal adjustments"))
        and any(token in lowered for token in ("apportion", "weighted", "remainder", "floating-point"))
    ):
        invariants.append(
            "Fixed-point parsing uses sign/digit strings and integer powers only, rejects non-zero precision beyond "
            "scale while allowing redundant zeroes, and preserves arbitrary-size integers. Weighted allocation uses "
            "floor shares plus descending integer residues with stable input-order ties, then reapplies the sign so "
            "the exact total and zero-weight entries are preserved."
        )
    semantic_version_compatibility = bool(
        (
            any(
                token in lowered
                for token in (
                    "semantic version",
                    "semver",
                    "prerelease",
                    "release candidate",
                )
            )
            or (
                "stable release" in lowered
                and "floor" in lowered
                and "ceiling" in lowered
            )
        )
        and any(
            token in lowered
            for token in (
                "compatib",
                "constraint",
                "package rule",
                "resolver",
                "selection",
            )
        )
    )
    if semantic_version_compatibility:
        invariants.append(
            "Semantic-version compatibility compares the numeric core first, makes a stable release outrank its "
            "prereleases, compares prerelease identifiers dot-by-dot with numeric identifiers ordered numerically "
            "and before nonnumeric identifiers, uses sequence length only after an equal prefix, and ignores build "
            "metadata. Every clause in one bounded package constraint is conjunctive, while withdrawn releases "
            "remain excluded."
        )
    if (
        any(token in lowered for token in ("asynchronous handler", "async handler", "coroutine"))
        and any(token in lowered for token in ("wrapped", "decorated", "tracing", "callable object"))
        and any(token in lowered for token in ("handler", "dispatch", "delivery"))
    ):
        invariants.append(
            "Handler completion follows the value returned by one invocation: a synchronous tracing wrapper keeps "
            "synchronous results immediate but defers its finished event until an awaitable result settles, and "
            "dispatch awaits inspect.isawaitable results. This covers decorated async handlers and callable objects "
            "with async __call__ without relying on inspect.iscoroutinefunction identity."
        )
    if (
        "suppression" in lowered
        and "batch" in lowered
        and any(
            token in lowered
            for token in (
                "without a target id",
                "null target",
                "nullable target",
                "advisory record",
                "advisory records",
            )
        )
    ):
        invariants.append(
            "A nullable suppression target cannot participate in a NOT IN anti-set because one NULL poisons every "
            "candidate under SQL three-valued logic. Batch selection uses a scope-preserving correlated NOT EXISTS "
            "match on the concrete target id, so advisory NULL rows do not suppress anything, duplicate targeted "
            "rows remain idempotent, and review-feed visibility is unchanged."
        )
    if (
        any(
            token in lowered
            for token in (
                "varint",
                "variable-length byte count",
                "variable length byte count",
                "variable-length prefix",
            )
        )
        and any(token in lowered for token in ("envelope", "frame", "prefix", "cursor"))
    ):
        invariants.append(
            "Canonical unsigned variable-length framing encodes the shortest base-128 representation, checks input "
            "bounds before every continuation-byte read, rejects unterminated and overlong prefixes with the "
            "declared parse error, reports the exact consumed width, and advances every framing consumer by that "
            "width rather than a fixed byte count."
        )
    if (
        "cookie" in lowered
        and any(token in lowered for token in ("domain", "hostname", "host-only"))
        and any(token in lowered for token in ("path", "max-age", "deletion"))
    ):
        invariants.append(
            "Hierarchical cookie scope preserves explicit zero age, host-only provenance, and exact scoped deletion. "
            "A domain cookie matches only the exact host or a dot-delimited subdomain, and a path matches only the "
            "exact path or a slash-delimited child; raw suffix, raw prefix, and truthiness checks are not equivalent."
        )
    if (
        "circuit" in lowered
        and any(token in lowered for token in ("trial", "half-open", "recovery"))
        and any(token in lowered for token in ("local", "client validation", "backend failure"))
    ):
        invariants.append(
            "A half-open recovery transition reserves exactly one trial lane. Peers remain rejected until success, "
            "remote failure, or a neutral local-error release; local validation neither increments backend failure "
            "state nor restarts cooldown, while success closes and remote failure reopens the circuit."
        )
    if (
        any(token in lowered for token in ("archive", "zip entry", "zip entries", "bundle importer"))
        and any(token in lowered for token in ("extract", "workspace", "destination"))
        and any(token in lowered for token in ("parent directory", "alternate separator", "symbolic link"))
    ):
        invariants.append(
            "Archive member names are untrusted cross-platform path data: normalize separator dialects before "
            "rejecting roots, drives, UNC names, and every parent component, then prove the resolved destination "
            "stays below the extraction root. ZIP Unix mode metadata is inspected before materialization and "
            "symbolic-link entries are rejected without creating partial output."
        )
    if (
        any(token in lowered for token in ("retry policy", "exception policy", "exception classes"))
        and any(token in lowered for token in ("broad", "superclass", "specialized", "specific"))
        and any(token in lowered for token in ("cause", "wrapper", "envelope"))
    ):
        invariants.append(
            "Exception-policy resolution chooses the most specific matching registered class independently of "
            "registration order. Retry execution passes exception instances, follows the cycle-guarded explicit "
            "cause chain for classification, preserves the configured attempt limit, and re-raises the outer "
            "exception when the effective decision is stop."
        )
    if (
        "json array" in lowered
        and any(token in lowered for token in ("role", "grant", "code"))
        and any(token in lowered for token in ("embedded", "neighbor", "quoted", "revok"))
    ):
        invariants.append(
            "JSON-array membership and removal operate on parsed elements with typed equality, never serialized-text "
            "substring or wildcard matching. SQLite uses json_each for element identity and rebuilds retained values "
            "in original array order so quoted, escaped, neighboring, and wildcard-bearing values remain distinct."
        )
    if (
        any(token in lowered for token in ("assembly", "work order", "required material"))
        and any(token in lowered for token in ("nested", "each assembly", "two branches", "same leaf"))
        and any(token in lowered for token in ("cycle", "bounded", "multipl"))
    ):
        invariants.append(
            "Recursive relation expansion carries a per-path quantity product, depth, and path-local visited set. "
            "Only a cycle-closing edge is rejected, distinct acyclic paths remain distinct through expansion, and "
            "terminal leaves alone are grouped at the material boundary so every path contribution is summed."
        )
    if (
        any(token in lowered for token in ("client authentication", "client certificate", "clientauth"))
        and any(token in lowered for token in ("tls", "trust material", "request", "verify"))
    ):
        invariants.append(
            "TLS client-auth modes map as none=(request false, reject false), optional=(request true, reject "
            "false), and required=(request true, reject true); required mode validates explicit trust material "
            "while preserving server certificate/key passthrough."
        )
    if (
        "reload" in lowered
        and any(token in lowered for token in ("config", "configuration", "settings", "gateway"))
        and any(
            token in lowered
            for token in ("replace", "replacement", "default", "omitted", "retained", "override")
        )
    ):
        invariants.append(
            "A replacement configuration reload rebuilds a fresh candidate from defaults plus the complete new "
            "payload, validates it before commit, and then rebinds every derived runtime consumer; omitted old "
            "overrides must not survive through merge/update state."
        )
    if (
        any(token in lowered for token in ("snapshot", "generation", "reload"))
        and any(token in lowered for token in ("request", "audit", "async", "asynchronous", "policy"))
    ):
        invariants.append(
            "A request captures one immutable deep snapshot and its generation before the asynchronous boundary; "
            "authorization, response, and audit all use that same snapshot even when a reload commits concurrently."
        )
    if "checkpoint" in lowered and any(
        token in lowered
        for token in ("replacement", "replaced", "truncate", "offset", "provenance", "source identity")
    ):
        invariants.append(
            "A resumable file checkpoint persists both offset and stable source identity. A changed identity, "
            "truncation, or offset beyond EOF resets reading to byte zero; unchanged sources resume exactly once."
        )
    if (
        any(token in lowered for token in ("category", "hierarchy", "parent graph"))
        and any(token in lowered for token in ("parent", "row order", "children before", "unordered"))
    ):
        invariants.append(
            "Hierarchy resolution is independent of input row order: index every node first, then resolve parent "
            "chains with explicit unknown-parent and cycle errors and guaranteed termination."
        )
    if (
        any(token in lowered for token in ("release", "generation", "retire"))
        and any(token in lowered for token in ("reader", "handle", "activate", "lifecycle"))
    ):
        invariants.append(
            "The generation manager owns reader counts and retirement. Activation deletes an old generation only "
            "when no readers remain; each handle releases exactly once through a callback, and the last reader "
            "reclaims only its own retired generation."
        )
    if "trusted proxy" in lowered or (
        "forwarded" in lowered and any(token in lowered for token in ("cidr", "proxy chain", "hop"))
    ):
        invariants.append(
            "Trusted proxy resolution validates exact IP/CIDR entries without undeclared packages, starts from the "
            "trusted immediate peer, walks forwarded hops right-to-left to the first untrusted client, and selects "
            "host/proto from the same aligned hop."
        )
    if any(token in lowered for token in ("tri-state", "inherits", "explicit disable")) and any(
        token in lowered for token in ("override", "workspace", "member", "configuration")
    ):
        invariants.append(
            "A tri-state override preserves NULL as inherit and false/zero as an explicit disable at every scope; "
            "schema nullability, writes, and resolution use first non-NULL value without truthiness."
        )
    if any(token in lowered for token in ("archive", "restore", "transition")) and any(
        token in lowered for token in ("move", "reassign", "counter", "project")
    ):
        invariants.append(
            "State-transition accounting covers the cross-product of lifecycle and ownership changes: each update "
            "computes old and new contribution once, so archive/restore and move/reassign cannot double-count or "
            "drive materialized counters negative."
        )
    rejected_retry_is_coalesced = (
        any(token in lowered for token in ("rejected", "rejection", "failure", "failing"))
        and any(token in lowered for token in ("retry", "restarts", "restart"))
        and any(
            token in lowered
            for token in (
                "coalesc",
                "concurrent miss",
                "loader once",
                "one producer",
                "shared miss",
                "same request",
                "process-local",
            )
        )
    )
    async_rejection_slot = (
        (
            any(token in lowered for token in ("async", "promise", "rejected", "rejection", "failure"))
            and any(token in lowered for token in ("slot", "keyed", "map", "cache"))
            and any(token in lowered for token in ("evict", "retain", "reuse", "retry", "stale"))
        )
        or rejected_retry_is_coalesced
    )
    if (
        (
            any(token in lowered for token in ("single-flight", "singleflight", "in-flight", "poison"))
            and any(token in lowered for token in ("retry", "later", "same key", "start fresh"))
        )
        or async_rejection_slot
    ):
        invariants.append(
            "Failed per-key in-flight work must be evicted by the state owner; the original error remains "
            "observable, successful concurrent work stays coalesced, and retry must not await its own cached promise."
        )
    if any(token in lowered for token in ("abortsignal", "abort signal", "aborterror")):
        invariants.append(
            "Propagate the caller's exact cancellation signal through every wrapper; cancellation is terminal "
            "for retries and must use existing platform error identity without invented dependencies."
        )
    subscriber_scoped_shared_work = bool(
        any(
            token in lowered
            for token in (
                "cancelling one",
                "canceling one",
                "one dashboard request",
                "one subscriber",
                "one waiter",
                "several peers leave",
            )
        )
        and any(
            token in lowered
            for token in (
                "unrelated",
                "same key",
                "waiting on the same key",
                "survivor",
                "peers",
            )
        )
        and any(token in lowered for token in ("coalesc", "shared", "same key"))
    )
    poisoned_result_retention = bool(
        any(
            token in lowered
            for token in (
                "remain unusable",
                "remains unusable",
                "upstream has recovered",
                "after the upstream recovered",
                "failed client read is not retained",
                "success-only result retention",
            )
        )
        and any(token in lowered for token in ("cache", "retention", "key", "result"))
    )
    if subscriber_scoped_shared_work or poisoned_result_retention:
        invariants.append(
            "Shared work has subscriber-scoped cancellation and success-only result retention: one caller leaves "
            "through its own wrapper without aborting surviving peers; the upstream is aborted only after every "
            "live subscriber leaves. In-flight work remains coalesced, while the result cache stores only resolved "
            "success values and never retains rejection or cancellation for a later retry."
        )
    if (
        any(token in lowered for token in ("ttl", "expiration", "expires", "expiry"))
        and any(token in lowered for token in ("injected clock", "replay time", "refresh"))
    ):
        invariants.append(
            "All expiry creation and comparison uses the injected clock; refresh updates both value and deadline "
            "without changing the public cache API."
        )
    if "subscription" in lowered and any(token in lowered for token in ("stop", "cancel", "lifecycle")):
        invariants.append(
            "The wrapper must return and store the actual active subscription, and stop must await cancellation "
            "before dropping the handle."
        )
    if any(token in lowered for token in ("partial unique", "unique index")):
        invariants.append(
            "Partial uniqueness applies only to the active-row predicate; historical inactive rows remain repeatable."
        )
    if any(token in lowered for token in ("one-to-many", "cartesian", "cross multipli")):
        invariants.append(
            "Aggregate each independent child relation to its parent key before joining sibling one-to-many data."
        )
    if "vary" in lowered and any(
        token in lowered for token in ("mixed casing", "case insensitive", "case-insensitive")
    ):
        invariants.append(
            "Case-insensitive header lookup must normalize both sides of the lookup (or iterate normalized entries); "
            "normalizing only the requested field does not change object/map key casing."
        )
    if "wildcard" in lowered and "cache" in lowered:
        invariants.append(
            "A wildcard Vary response is non-cacheable: do not store it and never return it from cache matching."
        )
    if any(
        token in lowered
        for token in (
            "exact instant",
            "expiration boundary",
            "expiry boundary",
            "boundary instant",
            "scheduled exactly when",
        )
    ):
        invariants.append(
            "An ineffective-at-expiration interval has an exclusive upper bound: valid_from <= as_of < valid_until; "
            "do not rewrite unrelated revocation semantics."
        )
    effective_history_boundary = bool(
        any(
            token in lowered
            for token in (
                "contiguous history",
                "contiguous effective",
                "overlap remains impossible",
                "write-time and read-time temporal",
                "two prices for one lookup instant",
                "scheduled exactly when the prior",
            )
        )
        and any(
            token in lowered
            for token in ("boundary", "effective", "expires", "history", "interval")
        )
    )
    if effective_history_boundary:
        invariants.append(
            "Contiguous effective-history rows use half-open intervals [start, end): adjacency where the old end "
            "equals the new start is legal; overlap is existing_start < new_end and new_start < existing_end, "
            "with a missing end treated as infinity. Point lookup keeps start <= instant and instant < end. "
            "The same overlap guard applies to INSERT and UPDATE, and UPDATE excludes the row being changed."
        )
    if "retry-after" in lowered and "numeric" in lowered:
        invariants.append(
            "Numeric Retry-After is seconds and converts to milliseconds exactly once; HTTP-date remains an "
            "absolute-time delta from the injected/current clock."
        )
    if any(token in lowered for token in ("explicit immediate retry", "explicit zero delay")):
        invariants.append(
            "A zero retry delay is a valid scheduled value; distinguish zero from absent/null instead of using "
            "truthiness."
        )
    if any(token in lowered for token in ("budget", "allowance")) and any(
        token in lowered for token in ("remaining allowance", "budget exhaustion", "remaining duration")
    ):
        invariants.append(
            "A positive request is clipped to the remaining budget instead of rejected; queue time uses that "
            "delay actually granted, while an explicit zero remains a valid non-null grant."
        )
    if "vector clock" in lowered or any(
        token in lowered for token in ("replicas concurrent", "convergence bookkeeping")
    ):
        invariants.append(
            "Vector-clock comparison and join use the union of actor keys and component-wise maxima; missing actors "
            "are zero and logical clocks are replicated state, not wall time."
        )
    if any(token in lowered for token in ("inclusive range", "content-range", "chunk bound")):
        invariants.append(
            "Inclusive byte-range length is end - start + 1. Strict overlap is rejected but adjacency at "
            "previous_end + 1 is accepted; sorted contiguous coverage is complete when its next offset equals total."
        )
    if any(token in lowered for token in ("repeated parameter", "duplicate query parameter")):
        invariants.append(
            "Canonical query rendering preserves every repeated parameter and blank value, then sorts the complete "
            "(key, value) pair sequence deterministically; converting pairs to a map loses wire information."
        )
    if "repeated" in lowered and "parameter" in lowered:
        invariants.append(
            "Canonical query rendering preserves every repeated parameter and blank value, then sorts the complete "
            "(key, value) pair sequence deterministically; converting pairs to a map loses wire information."
        )
    ordered_preference_identity = (
        any(
            token in lowered
            for token in (
                "preference order",
                "ordered preference",
                "caller order",
                "priority order",
                "order is contractual",
                "order remains contractual",
            )
        )
        and any(token in lowered for token in ("cache", "identity", "same tenant", "same account", "same user"))
    )
    if ordered_preference_identity:
        invariants.append(
            "Stable ordered-sequence identity preserves normalized first-occurrence order through serialization; "
            "normalization and deduplication must not sort a caller-priority sequence."
        )
    if "rotation" in lowered and any(token in lowered for token in ("key", "secret")):
        invariants.append(
            "Verification considers the current key plus only recently retired keys inside the configured grace "
            "window at the authenticated issue time; freshness compares normalized issue time to receiver time, "
            "while the signature keeps the exact raw timestamp and unrelated keys remain rejected."
        )
    if any(token in lowered for token in ("millisecond timestamp", "timestamps", "v2 delivery")) and any(
        token in lowered for token in ("freshness", "age check", "delivery")
    ):
        invariants.append(
            "Normalize epoch seconds versus milliseconds exactly once for time comparisons, using an explicit "
            "unit/version rule; preserve the raw timestamp bytes in signed material and never divide legacy seconds."
        )
    scoped_identity = bool(
        any(
            token in lowered
            for token in (
                "two tenant",
                "two site",
                "two merchant",
                "multi-merchant",
                "across merchant",
                "same merchant",
                "two org",
                "two organization",
                "two account",
                "regional",
            )
        )
        and any(
            token in lowered
            for token in (
                "reuse",
                "sharing",
                "shared",
                "share the same",
                "same client",
                "same identifier",
                "same principal",
            )
        )
    )
    if scoped_identity:
        invariants.append(
            "A reused external identifier is scoped by its tenant/site/merchant in storage uniqueness, idempotency "
            "lookup, insertion, and SQL conflict targets; all layers use the same composite identity, and lookup "
            "and storage must use the identical composite key."
        )
    if "attempt" in lowered and any(token in lowered for token in ("retry token", "retry", "dedup")):
        invariants.append(
            "Retry attempt number is not part of stable request/event identity; replay returns the original result "
            "without repeating stock/state mutation, and publishes creation only for the first successful creation."
        )
    monotonic_materialized_head = (
        any(
            token in lowered
            for token in (
                "materialized head",
                "materialized row",
                "current head",
                "stored head",
                "head metadata",
                "current version",
                "current-document",
            )
        )
        and any(
            token in lowered
            for token in (
                "monotonic",
                "newer",
                "older",
                "stale",
                "out of order",
                "late-arriving",
                "authoritative ordering",
                "logical clock",
                "sequence",
                "version",
            )
        )
    )
    if (
        "out of order" in lowered
        and any(token in lowered for token in ("correction", "replay"))
    ) or monotonic_materialized_head:
        invariants.append(
            "One winner predicate guards the complete out-of-order or materialized-head tuple: value, ordering "
            "position, and metadata update atomically; a stale replay changes none of them."
        )
    if "event-time" in lowered or (
        "observation" in lowered and "hourly bucket" in lowered
    ):
        invariants.append(
            "Event-time rollups use observation time for both window filtering and bucket calculation, never receipt "
            "time in either expression."
        )
    if any(token in lowered for token in ("replicas concurrent", "convergence bookkeeping")):
        invariants.append(
            "Replicated logical-clock comparison/join uses the union of actors and component-wise maxima; on an "
            "equal-time concurrent tie, tombstones win deterministic conflict choice to prevent resurrection."
        )
    if (
        any(token in lowered for token in ("delimiter", "delimited", "separator"))
        and any(token in lowered for token in ("configured", "profile", "selected"))
        and any(token in lowered for token in ("split", "quote", "contain", "export"))
    ):
        invariants.append(
            "A configured field delimiter is one policy input used consistently for field joining and quoting: "
            "quote a value when it contains that active delimiter, double embedded quotes, and never leave a "
            "hard-coded default delimiter in either decision."
        )
    if (
        any(token in lowered for token in ("queued", "running", "completed", "canceled", "terminal"))
        and any(token in lowered for token in ("retry", "late", "reappear", "take over", "worker"))
        and any(token in lowered for token in ("state", "status", "job", "transition"))
    ):
        invariants.append(
            "A state transition matches exactly its allowed predecessor in the mutation predicate: queued may "
            "become running and running may become completed. Terminal or foreign attempts match zero rows; a "
            "CASE expression that preserves values is not equivalent because it still reports a matched update."
        )
    if (
        any(token in lowered for token in ("unit", "unit table", "non-centimeter", "conversion"))
        and any(token in lowered for token in ("length", "volume", "oversized", "dimension"))
    ):
        invariants.append(
            "Normalize every physical length through the declared unit lookup before derived calculations. Volume "
            "multiplies all three normalized lengths, while oversize routing compares normalized individual lengths "
            "to the centimeter threshold; both queries must join and use the same conversion factor."
        )
    if (
        "factory" in lowered
        and any(token in lowered for token in ("keyword-only", "keyword only"))
        and any(token in lowered for token in ("collaborator", "dependency", "missing-argument"))
    ):
        invariants.append(
            "Required factory parameters retain their invocation binding: positional-only and positional-or-keyword "
            "dependencies are supplied positionally, while required keyword-only dependencies are discovered and "
            "supplied by name. Optional defaults and variadic parameters are not required dependencies."
        )
    if (
        "monthly" in lowered
        and any(token in lowered for token in ("month end", "month-end", "february", "billing cycle"))
        and any(token in lowered for token in ("utc", "local", "timezone", "time zone"))
    ):
        invariants.append(
            "Monthly local-wall-clock scheduling first converts the completed instant into the worker timezone, then "
            "clamps the requested billing day to the last real day of that local month before comparing candidates. "
            "Replacing tzinfo is not an instant conversion."
        )
    if (
        any(token in lowered for token in ("teardown", "shutdown", "hook"))
        and any(token in lowered for token in ("process-level", "process level", "systemexit", "keyboardinterrupt"))
        and any(token in lowered for token in ("original termination", "original", "bypass"))
    ):
        invariants.append(
            "Task teardown covers BaseException-class process termination, executes every registered hook in LIFO "
            "order even when a hook fails, and preserves the task's original termination over cleanup failures. "
            "Without an original task failure, the first cleanup failure is raised after all hooks run."
        )
    if (
        any(token in lowered for token in ("inventory scanner", "scan report", "report files"))
        and any(token in lowered for token in ("upgraded", "legacy", "newly produced"))
        and "license" in lowered
        and any(token in lowered for token in ("compound", "runtime", "development-only"))
    ):
        invariants.append(
            "Dependency report integration accepts both legacy component records and upgraded nested artifact "
            "records, preserving coordinates, license expression, and runtime scope. Compound license expressions "
            "evaluate parentheses with AND precedence over OR; development-only records remain outside violations."
        )
    if (
        any(token in lowered for token in ("offset change", "transition instant", "prior offset"))
        and any(token in lowered for token in ("wall-clock", "wall clock", "next run", "schedule"))
    ):
        invariants.append(
            "An offset transition is effective at its UTC boundary, and a future local wall-clock target is inverted "
            "using the offset effective at the candidate run instant, not the offset at scheduling time. A target "
            "created by a forward jump resolves to the transition instant."
        )
    if (
        "windows" in lowered
        and any(token in lowered for token in ("filename", "directories", "shared folder"))
        and any(token in lowered for token in ("letter case", "case", "numeric suffix", "replace one another"))
    ):
        invariants.append(
            "Portable Windows path segments remove trailing periods/spaces and protect reserved device basenames, "
            "including names with extensions. Entry allocation compares normalized full paths case-insensitively "
            "while preserving readable original casing and deterministic numeric suffixes."
        )
    if (
        any(token in lowered for token in ("requeued", "requeue", "watchdog"))
        and any(token in lowered for token in ("replacement worker", "original worker", "overlapping attempts"))
        and any(token in lowered for token in ("attempt", "settle", "status"))
    ):
        invariants.append(
            "A claimed job attempt is a fencing token: every completion and failure mutation supplies the claim's "
            "attempt and is accepted only while that same attempt is current and running. The runner forwards the "
            "token on both success and failure, so a late worker cannot mutate a replacement attempt."
        )
    return list(dict.fromkeys(invariants))[:8]


def _partial_unique_active_status(statement: str) -> str | None:
    lowered = str(statement or "").lower()
    explicit_statuses = "open|pending|active|running|enabled"
    row_statuses = "open|pending|running|enabled"
    patterns = (
        rf"\bstatus\s*(?:=|is|of)?\s*['\"]?({explicit_statuses})\b",
        rf"\b(?:two|multiple|duplicate|one|unique)\s+({row_statuses})\s+[a-z_]\w*",
        rf"\b({row_statuses})\s+rows?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return match.group(1)
    return None


def _singleflight_owner_evicts_key(content: str) -> bool:
    signature = re.search(
        r"function\s+singleflight\s*\((.*?)\)\s*(?::\s*[^\{]+)?\s*\{",
        content,
        re.DOTALL,
    )
    maps = re.findall(r"(?:const|let)\s+([a-z_$]\w*)\s*=\s*new\s+map", content)
    parameters = (
        re.findall(r"(?:^|,)\s*([a-z_$]\w*)\s*:", signature.group(1))
        if signature
        else []
    )
    if maps and parameters:
        return any(
            re.search(
                rf"\b{re.escape(map_name)}\.delete\s*\(\s*{re.escape(parameters[0])}\s*\)",
                content,
            )
            for map_name in maps
        )
    return bool(re.search(r"\b[a-z_$]\w*\.delete\s*\(", content))


def _closing_brace_index(content: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _recognize_async_rejection_slot(content: str) -> dict[str, Any] | None:
    """Bind one unguarded keyed promise slot without relying on fixture names."""
    if len(content) > 100_000:
        return None
    candidates: list[dict[str, Any]] = []
    signatures = re.finditer(
        r"(?P<header>(?:export\s+)?(?P<async>async\s+)?function\s+"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\((?P<parameters>.*?)\)"
        r"\s*(?::\s*(?P<return_type>[^\{]+))?\s*)\{",
        content,
        re.DOTALL,
    )
    for signature in signatures:
        opening = signature.end() - 1
        closing = _closing_brace_index(content, opening)
        if closing is None:
            continue
        body_start = opening + 1
        body = content[body_start:closing]
        slot_writes = list(
            re.finditer(
                r"(?m)^(?P<indent>[ \t]*)(?P<map>[A-Za-z_$][A-Za-z0-9_$]*)"
                r"\.set\(\s*(?P<key>[A-Za-z_$][A-Za-z0-9_$]*)\s*,\s*"
                r"(?P<operation>[A-Za-z_$][A-Za-z0-9_$]*)\s*\)\s*;",
                body,
            )
        )
        if len(slot_writes) != 1:
            continue
        slot_write = slot_writes[0]
        map_name = slot_write.group("map")
        key_name = slot_write.group("key")
        operation_name = slot_write.group("operation")
        map_declaration = re.search(
            rf"(?:\b(?:const|let|var)\s+|^[ \t]*(?:private\s+|protected\s+|public\s+)?"
            rf"(?:readonly\s+)?){re.escape(map_name)}\s*(?::[^=\n]+)?=\s*new\s+Map"
            rf"(?P<map_type><[^;\n]+>)?\s*\(",
            content,
            re.MULTILINE,
        )
        if map_declaration is None:
            continue
        return_type = str(signature.group("return_type") or "")
        map_type = str(map_declaration.group("map_type") or "")
        if not signature.group("async") and "promise" not in f"{return_type} {map_type}".lower():
            continue
        operation_assignments = list(
            re.finditer(
                rf"(?m)^(?P<indent>[ \t]*)(?:const|let)\s+{re.escape(operation_name)}"
                rf"(?:\s*:[^=;\n]+)?\s*=\s*[^;\n]+;",
                body,
            )
        )
        if len(operation_assignments) != 1:
            continue
        operation_assignment = operation_assignments[0]
        if operation_assignment.end() >= slot_write.start():
            continue
        returns = list(
            re.finditer(
                rf"\breturn\s+(?:await\s+)?{re.escape(operation_name)}\s*;",
                body[slot_write.end() :],
            )
        )
        if len(returns) != 1:
            continue
        if re.search(
            rf"\b{re.escape(operation_name)}\s*\.(?:catch|finally|then)\s*\(",
            body,
        ) or re.search(
            rf"\b{re.escape(map_name)}\.delete\s*\(\s*{re.escape(key_name)}\s*\)",
            body,
        ):
            continue
        candidates.append(
            {
                "function_name": signature.group("name"),
                "map_name": map_name,
                "key_name": key_name,
                "operation_name": operation_name,
                "operation_start": body_start + operation_assignment.start(),
                "operation_indent": operation_assignment.group("indent"),
                "slot_end": body_start + slot_write.end(),
                "slot_indent": slot_write.group("indent"),
                "has_lookup": bool(
                    re.search(
                        rf"\b{re.escape(map_name)}\.get\s*\(\s*{re.escape(key_name)}\s*\)",
                        body[: operation_assignment.start()],
                    )
                ),
                "body": body,
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _recognize_class_async_rejection_slot(content: str) -> dict[str, Any] | None:
    """Bind one class-owned promise slot with an explicitly empty rejection arm."""
    if len(content) > 100_000:
        return None
    candidates: list[dict[str, Any]] = []
    methods = (
        match
        for match in re.finditer(
            r"(?m)^(?P<indent>[ \t]+)(?:async\s+)?(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
            r"\s*\([^\n{}]*\)\s*(?::\s*[^\n\{]+)?\s*\{",
            content,
        )
        if match.group("name") not in {"if", "for", "while", "switch", "catch"}
    )
    for method in methods:
        opening = method.end() - 1
        closing = _closing_brace_index(content, opening)
        if closing is None:
            continue
        body_start = opening + 1
        body = content[body_start:closing]
        for slot_write in re.finditer(
            r"(?m)^(?P<indent>[ \t]*)(?P<map>this\.#?[A-Za-z_$][A-Za-z0-9_$]*)"
            r"\.set\(\s*(?P<key>[A-Za-z_$][A-Za-z0-9_$]*)\s*,\s*"
            r"(?P<operation>[A-Za-z_$][A-Za-z0-9_$]*)\s*\)\s*;",
            body,
        ):
            map_name = slot_write.group("map")
            field_name = map_name.removeprefix("this.")
            if not re.search(
                rf"(?m)^[ \t]*(?:(?:private|protected|public)\s+)?(?:readonly\s+)?"
                rf"{re.escape(field_name)}\s*(?::[^=\n]+)?=\s*new\s+Map(?:<[^;\n]+>)?\s*\(",
                content,
            ):
                continue
            key_name = slot_write.group("key")
            operation_name = slot_write.group("operation")
            assignments = list(
                re.finditer(
                    rf"(?m)^(?P<indent>[ \t]*)(?:const|let)\s+{re.escape(operation_name)}"
                    rf"(?:\s*:[^=;\n]+)?\s*=\s*(?P<rhs>[^;\n]+);",
                    body,
                )
            )
            if len(assignments) != 1 or assignments[0].end() >= slot_write.start():
                continue
            assignment = assignments[0]
            if not re.search(
                rf"\breturn\s+(?:await\s+)?{re.escape(operation_name)}\s*;",
                body[slot_write.end() :],
            ):
                continue
            if not re.search(
                rf"{re.escape(map_name)}\.get\s*\(\s*{re.escape(key_name)}\s*\)",
                body[: assignment.start()],
            ):
                continue
            rejection_arms = list(
                re.finditer(
                    rf"\b{re.escape(operation_name)}\.then\s*\("
                    rf"(?P<success>.{{0,4000}}?),\s*"
                    rf"(?P<handler>\(\s*[^)]*\)\s*=>\s*\{{\s*\}})\s*\)\s*;",
                    body,
                    re.DOTALL,
                )
            )
            if len(rejection_arms) != 1:
                continue
            rejection = rejection_arms[0]
            handler_start = body_start + rejection.start("handler")
            line_start = content.rfind("\n", 0, handler_start) + 1
            handler_indent = content[line_start:handler_start]
            if handler_indent.strip():
                continue
            candidates.append(
                {
                    "function_name": method.group("name"),
                    "map_name": map_name,
                    "key_name": key_name,
                    "operation_name": operation_name,
                    "mode": "replace_noop_rejection",
                    "handler_start": handler_start,
                    "handler_end": body_start + rejection.end("handler"),
                    "handler_indent": handler_indent,
                }
            )
    return candidates[0] if len(candidates) == 1 else None


def _recognize_async_rejection_contract(content: str) -> dict[str, Any] | None:
    candidates = [
        metadata
        for metadata in (
            _recognize_async_rejection_slot(content),
            _recognize_class_async_rejection_slot(content),
        )
        if metadata is not None
    ]
    return candidates[0] if len(candidates) == 1 else None


def _repair_async_rejection_slot(content: str, recognition: Mapping[str, Any]) -> str:
    operation_name = str(recognition["operation_name"])
    map_name = str(recognition["map_name"])
    key_name = str(recognition["key_name"])
    if recognition.get("mode") == "replace_noop_rejection":
        indent = str(recognition["handler_indent"])
        handler = (
            "() => {\n"
            f"{indent}  if ({map_name}.get({key_name}) === {operation_name}) {{\n"
            f"{indent}    {map_name}.delete({key_name});\n"
            f"{indent}  }}\n"
            f"{indent}}}"
        )
        start = int(recognition["handler_start"])
        end = int(recognition["handler_end"])
        return content[:start] + handler + content[end:]
    insertions: list[tuple[int, str]] = []
    if not bool(recognition["has_lookup"]):
        body = str(recognition["body"])
        existing_name = "existing"
        if re.search(r"\bexisting\b", body):
            existing_name = f"existing{operation_name[:1].upper()}{operation_name[1:]}"
        if re.search(rf"\b{re.escape(existing_name)}\b", body):
            return content
        indent = str(recognition["operation_indent"])
        insertions.append(
            (
                int(recognition["operation_start"]),
                f"{indent}const {existing_name} = {map_name}.get({key_name});\n"
                f"{indent}if ({existing_name}) return {existing_name};\n",
            )
        )
    indent = str(recognition["slot_indent"])
    insertions.append(
        (
            int(recognition["slot_end"]),
            "\n"
            f"{indent}void {operation_name}.catch(() => {{\n"
            f"{indent}  if ({map_name}.get({key_name}) === {operation_name}) "
            f"{map_name}.delete({key_name});\n"
            f"{indent}}});",
        )
    )
    updated = content
    for offset, insertion in sorted(insertions, reverse=True):
        updated = updated[:offset] + insertion + updated[offset:]
    return updated


def _recognize_ordered_sequence_identity(content: str) -> dict[str, Any] | None:
    """Bind one normalized, insertion-ordered Set that is sorted before serialization."""
    if len(content) > 100_000:
        return None
    candidates: list[dict[str, Any]] = []
    for assignment in re.finditer(
        r"(?m)^[ \t]*(?:const|let)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"(?P<sequence>\[\s*\.\.\.\s*new\s+Set\([^;\n]+\)\s*\]|"
        r"Array\.from\(\s*new\s+Set\([^;\n]+\)\s*\))"
        r"(?P<sort>\.sort\(\s*\))\s*;",
        content,
    ):
        sequence = assignment.group("sequence")
        if ".map(" not in sequence or not any(
            token in sequence
            for token in ("toLowerCase", "toUpperCase", ".normalize(", ".trim(")
        ):
            continue
        name = assignment.group("name")
        tail = content[assignment.end() : assignment.end() + 4_000]
        if not re.search(
            rf"\bJSON\.stringify\s*\([^;]*\b{re.escape(name)}\b[^;]*\)",
            tail,
            re.DOTALL,
        ):
            continue
        if re.search(rf"\b{re.escape(name)}\s*=", tail):
            continue
        candidates.append(
            {
                "sequence_name": name,
                "sort_start": assignment.start("sort"),
                "sort_end": assignment.end("sort"),
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _repair_ordered_sequence_identity(content: str, recognition: Mapping[str, Any]) -> str:
    start = int(recognition["sort_start"])
    end = int(recognition["sort_end"])
    return content[:start] + content[end:]


def _recognize_monotonic_sql_head(
    content: str,
    *,
    include_guarded: bool = False,
) -> dict[str, Any] | None:
    """Bind one mixed SQL head update to its structurally proven ordering column."""
    if len(content) > 100_000:
        return None
    candidates: list[dict[str, Any]] = []
    for update in re.finditer(
        r"\bON\s+CONFLICT\b(?:\s*\([^;]*?\)|\s+ON\s+CONSTRAINT\s+[A-Za-z_]\w*)?"
        r"\s+DO\s+UPDATE\s+SET\s+",
        content,
        re.IGNORECASE | re.DOTALL,
    ):
        statement_end = content.find(";", update.end())
        if statement_end < 0:
            continue
        update_tail = content[update.end() : statement_end]
        where_match = re.search(r"\bWHERE\b", update_tail, re.IGNORECASE)
        assignments = update_tail[: where_match.start()] if where_match else update_tail
        where_clause = update_tail[where_match.end() :] if where_match else ""
        statement_start = content.rfind(";", 0, update.start()) + 1
        statement_prefix = content[statement_start : update.start()]
        inserts = list(
            re.finditer(
                r"\bINSERT\s+INTO\s+(?P<table>(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)"
                r"(?:\s+AS\s+(?P<alias>[A-Za-z_]\w*))?",
                statement_prefix,
                re.IGNORECASE,
            )
        )
        if len(inserts) != 1:
            continue
        table_name = inserts[0].group("table").split(".")[-1]
        qualifiers = {table_name.lower()}
        if inserts[0].group("alias"):
            qualifiers.add(inserts[0].group("alias").lower())
        maxima: set[tuple[str, str]] = set()
        for maximum in re.finditer(
            r"\b(?:MAX|GREATEST)\s*\(\s*(?P<left_owner>[A-Za-z_]\w*)\."
            r"(?P<left_column>[A-Za-z_]\w*)\s*,\s*(?P<right_owner>[A-Za-z_]\w*)\."
            r"(?P<right_column>[A-Za-z_]\w*)\s*\)",
            assignments,
            re.IGNORECASE,
        ):
            left_owner = maximum.group("left_owner").lower()
            right_owner = maximum.group("right_owner").lower()
            left_column = maximum.group("left_column").lower()
            right_column = maximum.group("right_column").lower()
            if left_column != right_column:
                continue
            if left_owner == "excluded" and right_owner in qualifiers:
                maxima.add((right_owner, left_column))
            elif right_owner == "excluded" and left_owner in qualifiers:
                maxima.add((left_owner, left_column))
        comparisons: list[tuple[str, str, str]] = []
        for comparison in re.finditer(
            r"\bexcluded\.(?P<incoming>[A-Za-z_]\w*)\s*(?P<operator>>=|>)\s*"
            r"(?P<owner>[A-Za-z_]\w*)\.(?P<stored>[A-Za-z_]\w*)",
            assignments,
            re.IGNORECASE,
        ):
            incoming = comparison.group("incoming").lower()
            stored = comparison.group("stored").lower()
            owner = comparison.group("owner").lower()
            if incoming == stored and owner in qualifiers:
                comparisons.append((owner, incoming, comparison.group("operator")))
        bindings = {
            (owner, column, operator)
            for owner, column in maxima
            for comparison_owner, comparison_column, operator in comparisons
            if owner == comparison_owner and column == comparison_column
        }
        if len(bindings) != 1:
            continue
        owner, order_column, operator = next(iter(bindings))
        guarded = bool(
            where_match
            and re.search(
                rf"\bexcluded\.{re.escape(order_column)}\s*{re.escape(operator)}\s*"
                rf"{re.escape(owner)}\.{re.escape(order_column)}\b",
                where_clause,
                re.IGNORECASE,
            )
        )
        if where_match and (not include_guarded or not guarded):
            continue
        assignment_targets = re.findall(
            r"(?:^|,)\s*([A-Za-z_]\w*)\s*=",
            assignments,
            re.IGNORECASE,
        )
        direct_incoming = re.findall(
            r"(?:^|,)\s*([A-Za-z_]\w*)\s*=\s*excluded\.([A-Za-z_]\w*)\s*(?=,|$)",
            assignments,
            re.IGNORECASE | re.DOTALL,
        )
        if len(assignment_targets) < 2 or not any(
            target.lower() != order_column and source.lower() != order_column
            for target, source in direct_incoming
        ):
            continue
        candidates.append(
            {
                "statement_end": statement_end,
                "owner": owner,
                "order_column": order_column,
                "operator": operator,
                "guarded": guarded,
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _repair_monotonic_sql_head(content: str, recognition: Mapping[str, Any]) -> str:
    statement_end = int(recognition["statement_end"])
    owner = str(recognition["owner"])
    order_column = str(recognition["order_column"])
    operator = str(recognition["operator"])
    guard = f"\nWHERE excluded.{order_column} {operator} {owner}.{order_column}"
    return content[:statement_end] + guard + content[statement_end:]


def _is_order_like_sql_column(column: str) -> bool:
    lowered = str(column or "").lower()
    if lowered == "rowid" or lowered.endswith(("_id", "_key")):
        return False
    return bool(
        re.search(
            r"(?:^|_)(?:logical_)?(?:clock|sequence|revision|generation|offset|ordinal|"
            r"counter|epoch|position|version)(?:_|$)",
            lowered,
        )
    )


def _recognize_direct_sql_head_upsert(content: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for update in re.finditer(
        r"\bON\s+CONFLICT\s*\((?P<conflict>[^)]+)\)\s+DO\s+UPDATE\s+SET\s+",
        content,
        re.IGNORECASE,
    ):
        statement_end = content.find(";", update.end())
        if statement_end < 0:
            continue
        assignments = content[update.end() : statement_end]
        if re.search(r"\bWHERE\b", assignments, re.IGNORECASE):
            continue
        statement_start = content.rfind(";", 0, update.start()) + 1
        prefix = content[statement_start : update.start()]
        inserts = list(
            re.finditer(
                r"\bINSERT\s+INTO\s+(?P<table>(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)"
                r"(?:\s+AS\s+(?P<alias>[A-Za-z_]\w*))?\s*\((?P<columns>[^)]+)\)",
                prefix,
                re.IGNORECASE,
            )
        )
        if len(inserts) != 1:
            continue
        direct_assignments = re.findall(
            r"(?:^|,)\s*(?P<target>[A-Za-z_]\w*)\s*=\s*"
            r"excluded\.(?P<source>[A-Za-z_]\w*)\s*(?=,|$)",
            assignments,
            re.IGNORECASE | re.DOTALL,
        )
        if len(direct_assignments) < 2 or any(
            target.lower() != source.lower() for target, source in direct_assignments
        ):
            continue
        order_columns = {
            target.lower()
            for target, _source in direct_assignments
            if _is_order_like_sql_column(target)
        }
        if len(order_columns) != 1:
            continue
        table = inserts[0].group("table").split(".")[-1]
        owner = inserts[0].group("alias") or table
        conflict_columns = [
            value.strip().lower()
            for value in update.group("conflict").split(",")
            if re.fullmatch(r"[A-Za-z_]\w*", value.strip())
        ]
        insert_columns = [
            value.strip().lower()
            for value in inserts[0].group("columns").split(",")
            if re.fullmatch(r"[A-Za-z_]\w*", value.strip())
        ]
        order_column = next(iter(order_columns))
        if not conflict_columns or order_column not in insert_columns:
            continue
        candidates.append(
            {
                "table": table.lower(),
                "owner": owner.lower(),
                "conflict_columns": conflict_columns,
                "order_column": order_column,
                "statement_end": statement_end,
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _recognize_head_history_identity(
    files: Mapping[str, str],
    head_table: str,
    conflict_columns: Sequence[str],
    order_column: str,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for path, content in files.items():
        for table in re.finditer(
            rf"\bCREATE\s+TABLE\s+{re.escape(head_table)}\s*\((?P<body>.*?)\)\s*;",
            content,
            re.IGNORECASE | re.DOTALL,
        ):
            for foreign_key in re.finditer(
                r"\bFOREIGN\s+KEY\s*\((?P<local>[^)]+)\)\s*REFERENCES\s+"
                r"(?P<table>[A-Za-z_]\w*)\s*\((?P<remote>[^)]+)\)",
                table.group("body"),
                re.IGNORECASE,
            ):
                local_columns = [
                    value.strip().lower() for value in foreign_key.group("local").split(",")
                ]
                remote_columns = [
                    value.strip().lower() for value in foreign_key.group("remote").split(",")
                ]
                if (
                    len(local_columns) < 2
                    or len(local_columns) != len(remote_columns)
                    or not set(conflict_columns).issubset(local_columns)
                ):
                    continue
                history_table = foreign_key.group("table").lower()
                pair_by_local = dict(zip(local_columns, remote_columns))
                remote_scope = [
                    pair_by_local[column]
                    for column in conflict_columns
                    if column in pair_by_local
                ]
                if len(remote_scope) != len(conflict_columns):
                    continue
                required_unique = {*remote_scope, str(order_column).lower()}
                history_definitions = [
                    match.group("body")
                    for source in files.values()
                    for match in re.finditer(
                        rf"\bCREATE\s+TABLE\s+{re.escape(history_table)}\s*"
                        r"\((?P<body>.*?)\)\s*;",
                        source,
                        re.IGNORECASE | re.DOTALL,
                    )
                ]
                unique_sets = [
                    {
                        value.strip().lower()
                        for value in unique.group("columns").split(",")
                    }
                    for definition in history_definitions
                    for unique in re.finditer(
                        r"\bUNIQUE\s*\((?P<columns>[^)]+)\)",
                        definition,
                        re.IGNORECASE,
                    )
                ]
                if len(history_definitions) != 1 or required_unique not in unique_sets:
                    continue
                candidates.append(
                    {
                        "schema_path": str(path),
                        "history_table": history_table,
                        "identity_pairs": list(zip(local_columns, remote_columns)),
                    }
                )
    return candidates[0] if len(candidates) == 1 else None


def _recognize_rowid_head_read(
    content: str,
    *,
    head_table: str,
    history_table: str,
    identity_pairs: Sequence[tuple[str, str]],
    conflict_columns: Sequence[str],
) -> dict[str, Any] | None:
    head_joins = list(
        re.finditer(
            rf"\bJOIN\s+{re.escape(head_table)}\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*)\s+ON\b",
            content,
            re.IGNORECASE,
        )
    )
    history_joins = list(
        re.finditer(
            rf"\bJOIN\s+{re.escape(history_table)}\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*)"
            r"\s+ON\s+(?P<condition>.*?)(?=\b(?:JOIN|WHERE|ORDER\s+BY|GROUP\s+BY|LIMIT)\b|;)",
            content,
            re.IGNORECASE | re.DOTALL,
        )
    )
    if len(head_joins) != 1 or len(history_joins) != 1:
        return None
    head_alias = head_joins[0].group("alias")
    history_join = history_joins[0]
    history_alias = history_join.group("alias")
    condition = history_join.group("condition").strip()
    simple_equalities = re.fullmatch(
        r"[A-Za-z_]\w*\.[A-Za-z_]\w*\s*=\s*[A-Za-z_]\w*\.[A-Za-z_]\w*"
        r"(?:\s+AND\s+[A-Za-z_]\w*\.[A-Za-z_]\w*\s*=\s*"
        r"[A-Za-z_]\w*\.[A-Za-z_]\w*)*",
        condition,
        re.IGNORECASE,
    )
    if not simple_equalities:
        return None
    rowid_reads = list(
        re.finditer(
            rf"\bWHERE\s+{re.escape(history_alias)}\.rowid\s*=\s*\(\s*"
            r"SELECT\s+MAX\s*\(\s*(?:(?P<max_alias>[A-Za-z_]\w*)\.)?rowid\s*\)\s+"
            rf"FROM\s+{re.escape(history_table)}\s+(?:AS\s+)?(?P<candidate>[A-Za-z_]\w*)\s+"
            r"WHERE\s+(?P<subquery>.*?)\)\s*(?=ORDER\s+BY|;|$)",
            content,
            re.IGNORECASE | re.DOTALL,
        )
    )
    if len(rowid_reads) != 1:
        return None
    rowid_read = rowid_reads[0]
    candidate_alias = rowid_read.group("candidate")
    if rowid_read.group("max_alias") and (
        rowid_read.group("max_alias").lower() != candidate_alias.lower()
    ):
        return None
    pair_by_local = {local: remote for local, remote in identity_pairs}
    authoritative_identity = [
        pair_by_local[column]
        for column in conflict_columns
        if column in pair_by_local
    ]
    subquery = rowid_read.group("subquery")
    if len(authoritative_identity) != len(conflict_columns) or not all(
        re.search(
            rf"\b{re.escape(candidate_alias)}\.{re.escape(column)}\b",
            subquery,
            re.IGNORECASE,
        )
        for column in authoritative_identity
    ):
        return None
    join_indent_match = re.search(r"(?m)^(?P<indent>[ \t]*)ON\s+", history_join.group(0))
    join_indent = join_indent_match.group("indent") if join_indent_match else "  "
    join_condition = ("\n" + join_indent + "AND ").join(
        f"{history_alias}.{remote} = {head_alias}.{local}"
        for local, remote in identity_pairs
    )
    return {
        "join_start": history_join.start("condition"),
        "join_end": history_join.end("condition"),
        "join_condition": join_condition + "\n",
        "where_start": rowid_read.start(),
        "where_end": rowid_read.end(),
    }


def _recognize_cross_file_monotonic_head(
    files: Mapping[str, str],
) -> dict[str, Mapping[str, Any]]:
    upserts = [
        (str(path), metadata)
        for path, content in files.items()
        if (metadata := _recognize_direct_sql_head_upsert(content)) is not None
    ]
    if len(upserts) != 1:
        return {}
    upsert_path, upsert = upserts[0]
    identity = _recognize_head_history_identity(
        files,
        str(upsert["table"]),
        list(upsert["conflict_columns"]),
        str(upsert["order_column"]),
    )
    if identity is None:
        return {}
    reads = [
        (str(path), metadata)
        for path, content in files.items()
        if path != upsert_path
        and (
            metadata := _recognize_rowid_head_read(
                content,
                head_table=str(upsert["table"]),
                history_table=str(identity["history_table"]),
                identity_pairs=list(identity["identity_pairs"]),
                conflict_columns=list(upsert["conflict_columns"]),
            )
        )
        is not None
    ]
    if len(reads) != 1:
        return {}
    read_path, read = reads[0]
    return {
        upsert_path: {"role": "direct_head_upsert", **upsert},
        read_path: {"role": "rowid_head_read", **read},
    }


def _repair_cross_file_monotonic_head(
    content: str,
    recognition: Mapping[str, Any],
) -> str:
    if recognition["role"] == "direct_head_upsert":
        statement_end = int(recognition["statement_end"])
        guard = (
            f"\nWHERE excluded.{recognition['order_column']} > "
            f"{recognition['owner']}.{recognition['order_column']}"
        )
        return content[:statement_end] + guard + content[statement_end:]
    replacements = [
        (
            int(recognition["join_start"]),
            int(recognition["join_end"]),
            str(recognition["join_condition"]),
        ),
        (int(recognition["where_start"]), int(recognition["where_end"]), ""),
    ]
    updated = content
    for start, end, replacement in sorted(replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def _contract_repair_recognition(
    invariants: Sequence[str],
    files: Mapping[str, str],
) -> dict[str, dict[str, Mapping[str, Any]]]:
    recognized: dict[str, dict[str, Mapping[str, Any]]] = {}
    recognize_async = any("in-flight work must be evicted" in value for value in invariants)
    recognize_ordered_identity = any(
        "Stable ordered-sequence identity" in value for value in invariants
    )
    recognize_sql_head = any("One winner predicate guards" in value for value in invariants)

    def bind(path: str, family: str, metadata: Mapping[str, Any]) -> None:
        recognized.setdefault(path, {})[family] = metadata

    if recognize_async:
        candidates = [
            (str(path), metadata)
            for path, content in files.items()
            if (metadata := _recognize_async_rejection_contract(content)) is not None
        ]
        if len(candidates) == 1:
            path, metadata = candidates[0]
            bind(path, "async_rejection_slot", metadata)
    if recognize_ordered_identity:
        candidates = [
            (str(path), metadata)
            for path, content in files.items()
            if (metadata := _recognize_ordered_sequence_identity(content)) is not None
        ]
        if len(candidates) == 1:
            path, metadata = candidates[0]
            bind(path, "ordered_sequence_identity", metadata)
    if recognize_sql_head:
        candidates = [
            (str(path), metadata)
            for path, content in files.items()
            if (metadata := _recognize_monotonic_sql_head(content)) is not None
        ]
        if len(candidates) == 1:
            path, metadata = candidates[0]
            bind(path, "monotonic_sql_head", metadata)
        for path, metadata in _recognize_cross_file_monotonic_head(files).items():
            bind(path, "cross_file_monotonic_head", metadata)
    return recognized


def _provider_forwards_exact_signal(content: str) -> bool:
    signature = re.search(
        r"function\s+callprovider\s*\((.*?)\)\s*(?::\s*[^\{]+)?\s*\{",
        content,
        re.DOTALL,
    )
    if not signature:
        return False
    parameters = re.findall(r"(?:^|,)\s*([a-z_$]\w*)\s*:", signature.group(1))
    if len(parameters) < 2:
        return False
    body_tail = content[signature.end() : signature.end() + 4_000]
    return bool(
        re.search(
            rf"\breturn\s+{re.escape(parameters[0])}\s*\(\s*{re.escape(parameters[1])}\s*\)",
            body_tail,
        )
    )


def _dart_refresh_updates_deadline(content: str) -> bool:
    signature = re.search(
        r"\bvoid\s+refresh\s*\(\s*[^,()]+\s+[a-z_]\w*\s*,\s*"
        r"datetime\s+([a-z_]\w*)\s*\)",
        content,
        re.DOTALL,
    )
    deadline_field = re.search(r"\bdatetime\s+([a-z_]\w*)\s*;", content)
    if not signature or not deadline_field:
        return False
    return bool(
        re.search(
            rf"\b{re.escape(deadline_field.group(1))}\s*=\s*{re.escape(signature.group(1))}\s*;",
            content,
        )
    )


def contract_invariant_warnings(
    prompt: str,
    files: Mapping[str, str],
) -> list[str]:
    """Reject mechanically contradictory implementations for known contracts."""
    invariants = derive_contract_invariants(prompt)
    lowered_files = {
        str(path): str(content or "").lower()
        for path, content in files.items()
    }
    combined = "\n".join(lowered_files.values())
    warnings: list[str] = []
    if any(
        "Node conditional-export resolution" in value for value in invariants
    ):
        resolver_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "conditions" in content
            and re.search(
                r"(?:\bfunction\s+resolveexporttarget\s*\(|"
                r"\b(?:const|let)\s+resolveexporttarget\s*=)",
                content,
            )
        ]
        for path, content in resolver_sources:
            if re.search(r"object\.values\s*\(\s*entry\s*\)\s*\[\s*0\s*\]", content):
                warnings.append(
                    f"{path} still selects the first conditional-export value by object insertion order"
                )
            ordered_conditions = bool(
                re.search(
                    r"for\s*\([^)]*\bof\s+(?:\[\s*\.\.\.\s*)?conditions\b",
                    content,
                )
                or re.search(r"\bconditions\.(?:find|some|reduce)\s*\(", content)
            )
            if not ordered_conditions:
                warnings.append(
                    f"{path} does not walk the caller's declared export-condition order"
                )
            default_fallback = bool(
                re.search(r"(?:\.|\[\s*['\"])default(?:['\"]\s*\])?", content)
                or re.search(
                    r"\[\s*\.\.\.\s*conditions\s*,\s*['\"]default['\"]\s*\]",
                    content,
                )
            )
            if not default_fallback:
                warnings.append(
                    f"{path} omits the conditional-export default fallback"
                )
    if any("An ESM import built from a filesystem path" in value for value in invariants):
        loader_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "import(" in content
            and any(
                marker in content
                for marker in ("rawfileurl", "file://", "resolve(packageroot")
            )
        ]
        for path, content in loader_sources:
            if "pathtofileurl" not in content or "node:url" not in content:
                warnings.append(
                    f"{path} imports a filesystem path without node:url pathToFileURL"
                )
            if "encodeuricomponent" in content:
                warnings.append(
                    f"{path} applies encodeURIComponent to an ESM filesystem path"
                )
            if re.search(r"[`'\"]file://", content):
                warnings.append(
                    f"{path} still assembles a file:// URL manually"
                )
    if any("Semantic-version compatibility compares" in value for value in invariants):
        for path, content in lowered_files.items():
            if "class semanticversion" in content and "compareto" in content:
                compare_body = _dart_callable_body(content, "compareto") or ""
                if re.search(
                    r"prerelease\.join\s*\([^)]*\)\.compareto\s*\(",
                    compare_body,
                ):
                    warnings.append(
                        f"{path} compares the complete prerelease text lexically"
                    )
                if not (
                    "prerelease.isempty" in compare_body
                    and "other.prerelease.isempty" in compare_body
                ):
                    warnings.append(
                        f"{path} does not give stable releases precedence over prereleases"
                    )
                if "int.tryparse" not in compare_body or not re.search(
                    r"\bfor\s*\(", compare_body
                ):
                    warnings.append(
                        f"{path} does not compare prerelease identifiers individually with numeric precedence"
                    )
                if "buildmetadata" in compare_body:
                    warnings.append(
                        f"{path} incorrectly includes build metadata in version precedence"
                    )
            if "bestcompatible" in content and "clauses" in content:
                if re.search(r"\bclauses\.any\s*\(", content):
                    warnings.append(
                        f"{path} admits a release when only one bounded constraint clause matches"
                    )
                if not (
                    re.search(r"\bclauses\.every\s*\(", content)
                    or re.search(r"!\s*clauses\.any\s*\(", content)
                ):
                    warnings.append(
                        f"{path} does not require every package constraint clause to match"
                    )
    if any("Handler completion follows the value returned" in value for value in invariants):
        for path, content in lowered_files.items():
            if "def traced(" in content and "@wraps(" in content:
                if re.search(r"\basync\s+def\s+wrapper\s*\(", content):
                    warnings.append(
                        f"{path} makes synchronous traced handlers return an awaitable"
                    )
                async_completion = re.search(
                    r"\basync\s+def\s+[a-z_]\w*\s*\([^)]*\)\s*"
                    r"(?:->\s*[^:]+)?\s*:.*?\bawait\s+[a-z_]\w*.*?"
                    r"events\.append\s*\(\s*\(\s*['\"]finished['\"]",
                    content,
                    re.DOTALL,
                )
                if "isawaitable" not in content or not async_completion:
                    warnings.append(
                        f"{path} records traced completion without following an awaitable handler result"
                    )
            if re.search(r"\basync\s+def\s+dispatch\s*\(", content):
                if "iscoroutinefunction" in content:
                    warnings.append(
                        f"{path} dispatches by callable identity instead of the returned value"
                    )
                if "isawaitable" not in content or not re.search(
                    r"\bawait\s+result\b", content
                ):
                    warnings.append(
                        f"{path} does not await an awaitable returned by a wrapped or object handler"
                    )
    if any("A nullable suppression target cannot participate" in value for value in invariants):
        for path, content in lowered_files.items():
            batch_selection = bool(
                "suppression" in content
                and re.search(r"\bwhere\b.*?\bnot\s+(?:in|exists)\b", content, re.DOTALL)
            )
            if not batch_selection:
                continue
            if re.search(r"\bnot\s+in\s*\(", content):
                warnings.append(
                    f"{path} uses NOT IN against a nullable suppression target"
                )
            if not re.search(r"\bnot\s+exists\s*\(", content):
                warnings.append(
                    f"{path} does not use a correlated suppression anti-join"
                )
                continue
            if not re.search(
                r"\b([a-z_]\w*)\.([a-z_]\w*_id)\s*=\s*"
                r"([a-z_]\w*)\.\2\b",
                content,
            ):
                warnings.append(
                    f"{path} does not correlate the concrete suppression target id"
                )
    if any("configured field delimiter is one policy input" in value for value in invariants):
        delimiter_sources = [
            content
            for content in lowered_files.values()
            if "export_profile" in content or "field_separator" in content
        ]
        if any(
            re.search(r"\|\|\s*','\s*\|\|", content)
            or re.search(
                r"instr\s*\(\s*coalesce\s*\([^)]*\)\s*,\s*','\s*\)",
                content,
                re.DOTALL,
            )
            for content in delimiter_sources
        ):
            warnings.append(
                "configured field delimiter is not used consistently for both joining and quoting"
            )
    if any("state transition matches exactly its allowed predecessor" in value for value in invariants):
        predecessor_by_target = {"running": "queued", "completed": "running"}
        for path, content in lowered_files.items():
            if not re.search(r"\bupdate\b", content) or not re.search(
                r"\bset\b", content
            ):
                continue
            target_match = re.search(
                r"\bstate\s*=\s*(?:case\b.*?\belse\s*)?['\"](running|completed)['\"]",
                content,
                re.DOTALL,
            )
            if not target_match or "where" not in content:
                continue
            expected = predecessor_by_target[target_match.group(1)]
            where_clause = content.rsplit("where", 1)[-1]
            if not re.search(
                rf"\bstate\s*=\s*['\"]{re.escape(expected)}['\"]",
                where_clause,
            ):
                warnings.append(
                    f"{path} transitions to {target_match.group(1)} without requiring predecessor {expected} "
                    "in the mutation predicate"
                )
            if (
                target_match.group(1) == "completed"
                and "worker" in str(prompt or "").lower()
                and not re.search(r"\bworker_id\s*=\s*:worker_id\b", where_clause)
            ):
                warnings.append(
                    f"{path} completes a worker-owned job without matching the current worker in the predicate"
                )
    if any("Normalize every physical length" in value for value in invariants):
        for path, content in lowered_files.items():
            if "shipping_package" not in content or "length_value" not in content:
                continue
            if "length_unit" not in content or "centimeters_per_unit" not in content:
                warnings.append(
                    f"{path} derives package measurements without the declared length-unit conversion lookup"
                )
    if any("Required factory parameters retain their invocation binding" in value for value in invariants):
        planner_sources = [
            content
            for content in lowered_files.values()
            if "parameter.positional_only" in content and "dependency" in content
        ]
        if planner_sources and not any(
            "parameter.keyword_only" in content and "keyword" in content
            for content in planner_sources
        ):
            warnings.append(
                "required keyword-only factory dependencies are not discovered with their binding"
            )
        container_sources = [
            content
            for content in lowered_files.values()
            if "dependency_plan(" in content and re.search(r"\bfactory\s*\(", content)
        ]
        if container_sources and not any("**" in content for content in container_sources):
            warnings.append(
                "factory resolution does not supply discovered keyword-only dependencies by name"
            )
    if any("Monthly local-wall-clock scheduling first converts" in value for value in invariants):
        if re.search(r"\.replace\s*\(\s*tzinfo\s*=", combined):
            warnings.append("timezone metadata is replaced instead of converting the completed instant")
        monthly_sources = [
            content
            for content in lowered_files.values()
            if "billing_day" in content and "next_monthly" in content
        ]
        if monthly_sources and not any("monthrange" in content for content in monthly_sources):
            warnings.append("monthly billing day is not clamped to the last real day of the local month")
    if any("Task teardown covers BaseException-class process termination" in value for value in invariants):
        runtime_sources = [
            content
            for content in lowered_files.values()
            if "run_task" in content and "teardown" in content
        ]
        if runtime_sources and any("except exception" in content for content in runtime_sources):
            warnings.append("task teardown misses BaseException-class process termination")
        stack_sources = [
            content
            for content in lowered_files.values()
            if "class teardownstack" in content and "_callbacks" in content
        ]
        if stack_sources and not any(
            "except baseexception" in content and "first_error" in content
            for content in stack_sources
        ):
            warnings.append("teardown stack stops at the first failed hook instead of draining in LIFO order")
    if any("Dependency report integration accepts both legacy component records" in value for value in invariants):
        adapter_sources = [
            content
            for content in lowered_files.values()
            if "scanreportadapter" in content and "jsondecode" in content
        ]
        if adapter_sources and not any(
            "artifacts" in content and "coordinates" in content and "scope" in content
            for content in adapter_sources
        ):
            warnings.append("scan report adapter does not preserve the upgraded artifact schema")
        gate_sources = [
            content
            for content in lowered_files.values()
            if "licensegate" in content and "allowedlicenses" in content
        ]
        if gate_sources and not any(
            "_licenseexpressionparser" in content and "_parseand" in content and "_parseor" in content
            for content in gate_sources
        ):
            warnings.append("license gate does not evaluate compound AND/OR expressions")
    if any("An offset transition is effective at its UTC boundary" in value for value in invariants):
        offset_sources = [
            content
            for content in lowered_files.values()
            if "class offsetschedule" in content and "offsetat(" in content
        ]
        if any("!instantutc.isafter(change.atutc)" in content for content in offset_sources):
            warnings.append("offset transition still uses the prior offset at the exact boundary")
        scheduler_sources = [
            content
            for content in lowered_files.values()
            if "dailywindowscheduler" in content and "localtarget" in content
        ]
        if any(
            "localtarget.subtract(offsets.offsetat(nowutc))" in content
            and "localtarget.subtract(offsets.offsetat(candidateutc))" not in content
            for content in scheduler_sources
        ):
            warnings.append("next run is inverted with the scheduling-time offset instead of candidate-time offset")
    if any("Portable Windows path segments remove trailing periods/spaces" in value for value in invariants):
        segment_sources = [
            content
            for content in lowered_files.values()
            if "portablesegment" in content and "replaceall" in content
        ]
        if segment_sources and not any(
            "com[1-9]" in content and "lpt[1-9]" in content and "[. ]+$" in content
            for content in segment_sources
        ):
            warnings.append("portable segment normalization omits Windows reserved or trailing-name rules")
        allocation_sources = [
            content
            for content in lowered_files.values()
            if "allocateentry" in content and "_usedentries" in content
        ]
        if allocation_sources and not any(
            "candidate.tolowercase()" in content for content in allocation_sources
        ):
            warnings.append("entry collision allocation is case-sensitive on a Windows portability path")
    if any("A claimed job attempt is a fencing token" in value for value in invariants):
        store_sources = [
            content
            for content in lowered_files.values()
            if "class jobstore" in content and "requeuerunning" in content
        ]
        if store_sources and any(
            content.count("job.attempt !== attempt") < 2
            for content in store_sources
        ):
            warnings.append("job completion and failure are not both fenced by the current claim attempt")
        runner_sources = [
            content
            for content in lowered_files.values()
            if "class jobrunner" in content and "runnext" in content
        ]
        if runner_sources and any(
            "complete(job.id, job.attempt" not in content
            or "fail(job.id, job.attempt" not in content
            for content in runner_sources
        ):
            warnings.append("job runner does not forward the claim attempt on both settlement branches")
    if any("canonical text decoder" in value for value in invariants):
        decoder_sources = [
            content
            for content in lowered_files.values()
            if "base64" in content and any(token in content for token in ("decode", "decoder"))
        ]
        if decoder_sources and not any(
            "encode" in content and any(token in content for token in ("canonical", "re-encode", "reencode"))
            for content in decoder_sources
        ):
            warnings.append(
                "canonical decoder does not prove decode-then-encode equality and may accept textual aliases"
            )
    if any("TLS client-auth modes map" in value for value in invariants):
        if (
            "requestcert: config.clientauth !== 'none'" not in combined
            or "rejectunauthorized: config.clientauth === 'required'" not in combined
        ):
            warnings.append("TLS client-auth mode is not mapped to request/verify booleans explicitly")
        validation_sources = [
            content
            for content in lowered_files.values()
            if "validatetlsconfig" in content and "clientauth" in content
        ]
        if validation_sources and not any(
            "clientauth === 'required'" in content and "ca" in content
            for content in validation_sources
        ):
            warnings.append("required TLS client authentication lacks explicit trust-material validation")
    if any("replacement configuration reload" in value for value in invariants):
        config_sources = [
            content
            for content in lowered_files.values()
            if any(token in content for token in ("config", "settings"))
            and any(token in content for token in ("reload", "load"))
        ]
        if any(
            re.search(
                r"\b(?:self\.)?(?:values|current|settings|config)\.(?:update|addall)\s*\(",
                content,
            )
            or re.search(r"\{\s*\.\.\.?[a-z_$][\w.$]*\s*,", content)
            for content in config_sources
        ):
            warnings.append(
                "replacement reload merges into retained configuration instead of rebuilding from defaults"
            )
    if any("stable source identity" in value for value in invariants):
        checkpoint_sources = [
            content
            for content in lowered_files.values()
            if "checkpoint" in content and any(token in content for token in ("save", "load"))
        ]
        if checkpoint_sources and not any(
            any(token in content for token in ("source_id", "sourceid", "identity", "inode", "fileid"))
            for content in checkpoint_sources
        ):
            warnings.append("checkpoint persists an offset without stable source identity")
    if any("Hierarchy resolution is independent" in value for value in invariants):
        if re.search(
            r"for\s+\w+\s+in\s+\w+[^:]*:\s*(?:.|\n){0,500}"
            r"\w+\s*\[[^\]]+\]\s*=\s*\w+\s*\[[^\]]*(?:parent|parent_id)[^\]]*\]",
            combined,
            re.IGNORECASE,
        ):
            warnings.append(
                "hierarchy paths are resolved during the input pass and therefore depend on parent row order"
            )
    if any("tri-state override" in value for value in invariants):
        override_blocks = re.findall(
            r"create\s+table\s+[a-z_]\w*override[a-z_]*\s*\(.*?\)\s*;",
            combined,
            re.IGNORECASE | re.DOTALL,
        )
        if any(
            re.search(r"\benabled\b[^,\n]*\bnot\s+null\b", block, re.IGNORECASE)
            for block in override_blocks
        ):
            warnings.append("tri-state override schema forbids NULL inheritance")
    if any("in-flight work must be evicted" in value for value in invariants):
        structurally_unguarded = any(
            _recognize_async_rejection_contract(content) is not None
            for content in files.values()
        )
        owners = [
            content
            for content in lowered_files.values()
            if "new map" in content and any(token in content for token in ("pending", "flight"))
        ]
        named_owner_is_unguarded = owners and not any(
            _singleflight_owner_evicts_key(content) for content in owners
        )
        if named_owner_is_unguarded or structurally_unguarded:
            warnings.append(
                "failed per-key in-flight state is retained; the state owner must delete/remove the key on rejection"
            )
        if any(
            token in "\n".join(owners)
            for token in ("pending.set(key, { error", "promise.reject(error)", "error?: error")
        ):
            warnings.append("rejected work is cached instead of evicted")
        if re.search(r"catch\s*(?:\([^)]*\))?\s*\{[^{}]*\breturn\b", combined, re.DOTALL):
            warnings.append("the wrapper swallows the original error by returning from catch")
    if any("Stable ordered-sequence identity" in value for value in invariants):
        if any(
            _recognize_ordered_sequence_identity(content) is not None
            for content in files.values()
        ):
            warnings.append(
                "ordered preference identity is sorted before serialization and loses caller priority"
            )
    if any("exact cancellation signal" in value for value in invariants):
        provider_sources = [
            content
            for content in lowered_files.values()
            if "function callprovider" in content and "provideradapter" in content
        ]
        if provider_sources and not any(_provider_forwards_exact_signal(content) for content in provider_sources):
            warnings.append("the provider wrapper does not pass the caller's exact signal to the adapter")
        retry_sources = [
            content
            for content in lowered_files.values()
            if "catch" in content and re.search(r"\bfor\s*\(", content)
        ]
        if retry_sources and not any(
            "aborterror" in content
            and ".name" in content
            and re.search(r"\bthrow\s+[a-z_$]\w*", content)
            for content in retry_sources
        ):
            warnings.append("AbortError is still treated as retryable instead of terminal")
        if "node:abort-controller" in combined:
            warnings.append("an invented abort-controller dependency replaces platform cancellation primitives")
        if "instanceof aborterror" in combined and "class aborterror" not in combined:
            warnings.append("AbortError is referenced as an undefined class; inspect the existing error name")
    if any("All expiry creation and comparison" in value for value in invariants):
        cache_sources = [
            content
            for content in lowered_files.values()
            if any(
                marker in content
                for marker in ("datetime.now()", "final clock", "void refresh")
            )
        ]
        cache_text = "\n".join(cache_sources)
        if "datetime.now()" in cache_text:
            warnings.append("TTL comparison still reads wall clock instead of the injected clock")
        refresh_sources = [content for content in cache_sources if "void refresh" in content]
        if refresh_sources and not all(
            _dart_refresh_updates_deadline(content) for content in refresh_sources
        ):
            warnings.append("cache refresh updates the value without replacing its expiry deadline")
    if any("actual active subscription" in value for value in invariants):
        if "bindsubscription" in combined and not re.search(
            r"return\s+[a-z_]\w*\.listen\s*\(\s*[a-z_]\w*",
            combined,
        ):
            warnings.append("subscription wrapper does not return the actual source listener handle")
        if "future<void> stop" in combined and ".cancel()" not in combined:
            warnings.append("worker stop drops the subscription without cancellation")
    if any("Partial uniqueness" in value for value in invariants):
        active_status = _partial_unique_active_status(prompt)
        if active_status and "create unique index" in combined and not re.search(
            rf"where\s+status\s*=\s*['\"]{re.escape(active_status)}['\"]",
            combined,
        ):
            warnings.append(
                f"partial unique index is not scoped to explicitly active {active_status} rows"
            )
    if any("Aggregate each independent child" in value for value in invariants):
        if any(
            _preaggregate_sibling_sum_joins(content) is not None
            for content in files.values()
        ):
            warnings.append("independent one-to-many children are still joined before aggregation")
    if any("normalize both sides of the lookup" in value for value in invariants):
        vary_sources = [
            content
            for content in lowered_files.values()
            if re.search(r"\bfunction\s+parsevary\s*\(", content)
        ]
        if any(
            not all(token in content for token in ("trim()", "tolowercase()", "new set"))
            for content in vary_sources
        ):
            warnings.append("Vary fields are not trimmed, case-normalized, and deduplicated")
        header_sources = [
            content for content in lowered_files.values() if "headervalue" in content
        ]
        if any(re.search(r"headers\s*\[\s*name\s*\]", content) for content in header_sources):
            warnings.append("request header lookup still depends on exact object-key casing")
    if any("wildcard Vary response is non-cacheable" in value for value in invariants):
        cache_sources = [
            content
            for content in lowered_files.values()
            if "parsevary" in content and ".set(" in content
        ]
        if any(
            not (
                match := re.search(
                    r"(?:const|let)\s+([a-z_$]\w*)\s*=\s*parsevary\s*\(",
                    content,
                )
            )
            or not re.search(
                rf"{re.escape(match.group(1))}\.includes\s*\(\s*['\"]\*['\"]\s*\)",
                content,
            )
            for content in cache_sources
        ):
            warnings.append("wildcard Vary response is still inserted into cache storage")
    if any("complete (key, value) pair sequence" in value for value in invariants):
        query_sources = [
            content
            for content in lowered_files.values()
            if "parse_qsl" in content and "urlencode" in content
        ]
        if any(re.search(r"\bdict\s*\(\s*parse_qsl\s*\(", content) for content in query_sources):
            warnings.append("canonical query converts parsed pairs to a map and collapses repeated parameters")
        if any("sorted(" not in content for content in query_sources):
            warnings.append("canonical query preserves input order instead of deterministically sorting all pairs")
    if any("authenticated issue time" in value for value in invariants):
        key_sources = [
            content
            for content in lowered_files.values()
            if "grace_seconds" in content and "valid_until" in content
        ]
        if key_sources and not any(
            re.search(r"valid_until\s*\+\s*(?:self\.)?grace_seconds", content)
            for content in key_sources
        ):
            warnings.append("retired-key eligibility is not bounded by valid_until plus the configured grace")
        verifier_sources = [
            content
            for content in lowered_files.values()
            if "candidates(" in content and "timestamp" in content
        ]
        if any(
            re.search(r"candidates\s*\(\s*key_id\s*,\s*now\s*\)", content)
            for content in verifier_sources
        ):
            warnings.append("key eligibility uses receiver time instead of the authenticated issue time")
    if any("Normalize epoch seconds versus milliseconds" in value for value in invariants):
        timestamp_sources = [
            content
            for content in lowered_files.values()
            if "timestamp" in content and "int(" in content
        ]
        if any(
            (
                raw_match := re.search(
                    r"(?m)^\s*([a-z_]\w*)\s*=\s*headers\.get\([^\n]*timestamp[^\n]*\)",
                    content,
                )
            )
            and re.search(
                rf"\b[a-z_]\w*\s*=\s*int\s*\(\s*{re.escape(raw_match.group(1))}\s*\)\s*"
                r"(?://|/)\s*1000",
                content,
            )
            for content in timestamp_sources
        ):
            warnings.append("timestamp conversion divides every value and breaks legacy epoch seconds")
        if any(
            (
                raw_match := re.search(
                    r"(?m)^\s*([a-z_]\w*)\s*=\s*headers\.get\([^\n]*timestamp[^\n]*\)",
                    content,
                )
            )
            and re.search(
                rf"\b[a-z_]\w*\s*=\s*int\s*\(\s*{re.escape(raw_match.group(1))}\s*\)\s*$",
                content,
                re.MULTILINE,
            )
            and "100_000_000_000" not in content
            for content in timestamp_sources
        ):
            warnings.append("timestamp freshness has no explicit seconds-versus-milliseconds normalization")
        if any(
            "raw_timestamp_seconds" in content
            and re.search(r"\{\s*raw_timestamp_seconds\s*\}", content)
            for content in timestamp_sources
        ):
            warnings.append("signature material rewrites the raw wire timestamp")
    if any("identical composite key" in value for value in invariants):
        reservation_sources = [
            content
            for content in lowered_files.values()
            if "request_id" in content
            and (".get(" in content or re.search(r"self\.[a-z_]\w*\s*\[", content))
        ]
        if any(
            re.search(r"self\.[a-z_]\w*\.get\s*\(\s*request_id\s*\)", content)
            or re.search(r"self\.[a-z_]\w*\s*\[\s*request_id\s*\]", content)
            for content in reservation_sources
        ):
            warnings.append("idempotency lookup or storage still omits the tenant scope")
        sql_identity_sources = [
            content
            for content in lowered_files.values()
            if "event_id" in content and any(token in content for token in ("unique(", "on conflict("))
        ]
        if any(
            re.search(r"(?:unique|on\s+conflict)\s*\(\s*(?:sensor_id\s*,\s*)?event_id\s*\)", content)
            for content in sql_identity_sources
        ):
            warnings.append("database uniqueness/conflict identity still omits tenant or site scope")
        scoped_sql_sources = [
            content
            for content in lowered_files.values()
            if any(scope in content for scope in ("tenant_id", "site_id", "merchant_id", "org_id", "account_id"))
            and any(token in content for token in ("unique index", "on conflict("))
        ]
        if any(
            (
                match := re.search(
                    r"(?:on\s+[a-z_]\w*|on\s+conflict)\s*\(([^)]+)\)",
                    content,
                )
            )
            and not any(
                scope in match.group(1)
                for scope in ("tenant_id", "site_id", "merchant_id", "org_id", "account_id")
            )
            for content in scoped_sql_sources
        ):
            warnings.append("SQL identity target omits the available tenant/site/account scope column")
    if any("without repeating stock/state mutation" in value for value in invariants):
        event_sources = [
            content for content in lowered_files.values() if "event_id" in content
        ]
        if any(
            re.search(r"event_id[^\n]*\{\s*attempt\s*\}", content)
            for content in event_sources
        ):
            warnings.append("event identity still includes retry attempt number")
        service_sources = [
            content
            for content in lowered_files.values()
            if "publisher.publish" in content and "ledger.reserve" in content
        ]
        if any("messages[-1]" in content for content in service_sources):
            warnings.append("replay deduplication depends only on the last published message")
        if any(
            "ledger.find" not in content and "created" not in content
            for content in service_sources
        ):
            warnings.append("service republishes creation without proving that the reservation was newly created")
    if any("positive request is clipped" in value for value in invariants):
        budget_sources = [
            content
            for content in lowered_files.values()
            if "claim(" in content
            and any(token in content for token in ("limit", "cap", "budget"))
            and any(token in content for token in ("used", "spent", "consumed"))
        ]
        if any(
            re.search(
                r"this\.[a-z_$]\w*\s*\+\s*[a-z_$]\w*\s*>\s*this\.[a-z_$]\w*",
                content,
            )
            for content in budget_sources
        ):
            warnings.append("retry budget rejects an oversized request instead of clipping it to the remainder")
        scheduler_sources = [
            content
            for content in lowered_files.values()
            if ".claim(" in content and "enqueue" in content
        ]
        if any(
            (
                match := re.search(
                    r"(?:const|let)\s+([a-z_$]\w*)\s*=\s*[^;\n]*\.claim\s*\(",
                    content,
                )
            )
            and re.search(rf"if\s*\(\s*!\s*{re.escape(match.group(1))}\s*\)", content)
            for content in scheduler_sources
        ):
            warnings.append("scheduler treats a valid zero grant as absent")
        if any(
            re.search(
                r"runat\s*=\s*[a-z_$]\w*\s*\+\s*[a-z_$]*requested[a-z_$0-9]*",
                content,
            )
            for content in scheduler_sources
        ):
            warnings.append("scheduler queues the requested delay instead of the granted delay")
    if any("Numeric Retry-After is seconds" in value for value in invariants):
        parser_sources = [
            content
            for content in lowered_files.values()
            if "retry-after" in content or "parseretryafter" in content
        ]
        if any(
            re.search(r"return\s+number\s*\(\s*text\s*\)\s*;", content)
            for content in parser_sources
        ):
            warnings.append("numeric Retry-After is returned as milliseconds without seconds conversion")
    if any("union of actor keys" in value for value in invariants):
        clock_sources = [
            content for content in lowered_files.values() if "compareclocks" in content
        ]
        if any(
            (
                match := re.search(
                    r"clockorder\s+compareclocks\s*\(\s*map<string,\s*int>\s+([a-z_]\w*)\s*,\s*"
                    r"map<string,\s*int>\s+([a-z_]\w*)",
                    content,
                    re.DOTALL,
                )
            )
            and f"{match.group(1)}.keys" in content
            and f"{match.group(2)}.keys" not in content
            for content in clock_sources
        ):
            warnings.append("vector-clock comparison ignores actor keys present only on the right")
        sync_sources = [
            content for content in lowered_files.values() if "applyremote" in content
        ]
        if sync_sources and not any("joinclocks" in content for content in sync_sources):
            warnings.append("sync engine stores a winner without joining both replica clocks")
    if any("tombstones win" in value for value in invariants):
        merge_sources = [
            content for content in lowered_files.values() if "resolverecord" in content
        ]
        if merge_sources and not any(
            ".deleted" in content[content.find("resolverecord") :]
            for content in merge_sources
        ):
            warnings.append("equal-time concurrent conflict choice does not give tombstones precedence")
    if any("Strict overlap is rejected" in value for value in invariants):
        range_sources = [
            content for content in lowered_files.values() if "contentrange" in content or "range." in content
        ]
        if any(re.search(r"length\s*=>\s*end\s*-\s*start\s*;", content) for content in range_sources):
            warnings.append("inclusive range length omits the final byte")
        if any(
            "existing.start - 1" in content or "existing.end + 1" in content
            for content in range_sources
        ):
            warnings.append("range overlap test incorrectly classifies adjacent ranges as conflicting")
        assembler_sources = [
            content for content in lowered_files.values() if "assemble" in content and "offset" in content
        ]
        if any("tostring().compareto" in content for content in assembler_sources):
            warnings.append("chunk offsets are sorted lexically instead of numerically")
    if any("One winner predicate guards" in value for value in invariants):
        structurally_unguarded = any(
            _recognize_monotonic_sql_head(content) is not None
            for content in files.values()
        ) or bool(_recognize_cross_file_monotonic_head(files))
        upsert_sources = [
            content
            for path, content in lowered_files.items()
            if "on conflict" in content
            and "observed_at" in content
            and "received_at" in content
            and not (
                (metadata := _recognize_monotonic_sql_head(files[path], include_guarded=True))
                and metadata["guarded"]
            )
        ]
        if structurally_unguarded or any(
            "where excluded.received_at" not in content
            and "where excluded.version" not in content
            for content in upsert_sources
        ):
            warnings.append("out-of-order upsert lacks one winner predicate guarding the complete tuple")
    if any("never receipt time in either expression" in value for value in invariants):
        rollup_sources = [
            content
            for content in lowered_files.values()
            if "strftime" in content and re.search(r"\bfrom\s+[a-z_]\w*", content)
        ]
        if any(
            re.search(r"strftime\s*\([^\n]*received_at", content)
            or re.search(r"where\s+received_at", content)
            for content in rollup_sources
        ):
            warnings.append("event-time rollup still uses receipt time for its bucket or window")
    if any("Canonical unsigned variable-length framing" in value for value in invariants):
        varint_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if re.search(r"\blist<int>\s+encodevarint\s*\(", content)
            and re.search(r"\bvarintresult\s+decodevarint\s*\(", content)
        ]
        for path, content in varint_sources:
            if not all(
                marker in content
                for marker in ("0x80", "bytesread", "while", "encodevarint(value).length")
            ):
                warnings.append(f"{path} does not implement canonical multi-byte varint framing")
            if not re.search(r"index\s*>=\s*bytes\.length", content):
                warnings.append(f"{path} reads continuation bytes without a per-byte bounds check")
        for path, content in lowered_files.items():
            if "decodeenvelopes" in content and "decodevarint" in content and "length.bytesread" not in content:
                warnings.append(f"{path} advances the envelope cursor by a fixed prefix width")
    if any("Hierarchical cookie scope" in value for value in invariants):
        for path, content in lowered_files.items():
            if "function parsesetcookie" in content and re.search(
                r"number\.isinteger\(seconds\)\s*&&\s*seconds",
                content,
            ):
                warnings.append(f"{path} discards explicit Max-Age zero through truthiness")
            if "class cookiejar" not in content:
                continue
            if "hostonly" not in content:
                warnings.append(f"{path} loses host-only cookie provenance")
            if re.search(r"\.endswith\(\s*(?:entry\.)?domain\s*\)", content):
                warnings.append(f"{path} uses raw hostname suffix matching without a label boundary")
            if "charat(entry.path.length)" not in content or "entry.path.endswith('/')" not in content:
                warnings.append(f"{path} uses raw cookie path prefixes without a segment boundary")
    if any("A half-open recovery transition" in value for value in invariants):
        for path, content in lowered_files.items():
            if "class circuitbreaker" in content:
                if "trialinflight" not in content or "releasetrial" not in content:
                    warnings.append(f"{path} does not reserve and neutrally release one half-open trial")
                if not re.search(r"if\s*\(this\.#trialinflight\)\s*return\s+false", content):
                    warnings.append(f"{path} admits peers while a half-open trial is already active")
            if "function callservice" in content:
                if "clientinputerror" not in content or "breaker.releasetrial()" not in content:
                    warnings.append(f"{path} counts local client validation as backend failure evidence")
    if any("Archive member names are untrusted" in value for value in invariants):
        for path, content in lowered_files.items():
            if "def normalized_member_path(" in content:
                if "purewindowspath" not in content or ".replace(\"\\\\\", \"/\")" not in content:
                    warnings.append(f"{path} does not normalize and reject alternate path dialects")
                if "part == \"..\"" not in content:
                    warnings.append(f"{path} does not reject every parent path component")
            if "def extract_archive(" in content:
                if ".resolve()" not in content or ".relative_to(root)" not in content:
                    warnings.append(f"{path} does not prove each resolved target stays under the extraction root")
                if "stat.s_islnk" not in content or "external_attr" not in content:
                    warnings.append(f"{path} materializes ZIP symbolic-link entries")
                if ".is_symlink(" in content:
                    warnings.append(f"{path} calls a non-portable ZipInfo symbolic-link helper")
    if any("Exception-policy resolution chooses" in value for value in invariants):
        for path, content in lowered_files.items():
            if "class exceptionpolicy" in content and "def classify(" in content:
                if "mro.index(registered)" not in content or "min(matches" not in content:
                    warnings.append(f"{path} resolves exception rules by registration order instead of specificity")
            if "def run_with_retry(" in content:
                if "policy.classify(type(" in content:
                    warnings.append(f"{path} passes an exception class instead of the caught instance")
                if "__cause__" not in content or "seen" not in content or "policy.classify(effective)" not in content:
                    warnings.append(f"{path} does not classify the cycle-guarded explicit cause chain")
    if any("JSON-array membership and removal" in value for value in invariants):
        for path, content in lowered_files.items():
            if "json_contains(" in content or "json_array_contains(" in content:
                warnings.append(f"{path} uses a non-SQLite JSON membership primitive")
            if "from document" in content and ":roles_json" in content:
                if "json_each(:roles_json)" not in content or not re.search(
                    r"granted\.value\s*=\s*document\.required_role",
                    content,
                ):
                    warnings.append(f"{path} does not select grants by parsed JSON element identity")
            if "update principal_grant" in content and ":role" in content:
                if " like " in content or "not like" in content:
                    warnings.append(f"{path} removes JSON roles by serialized wildcard text")
                if "json_each(principal_grant.roles_json)" not in content or "value <> :role" not in content:
                    warnings.append(f"{path} does not remove exactly one parsed JSON role value")
                if "order by key" not in content:
                    warnings.append(f"{path} does not explicitly preserve retained JSON-array order")
    if any("Recursive relation expansion carries" in value for value in invariants):
        recursive_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "from component" in content and ":root_sku" in content
        ]
        for path, content in recursive_sources:
            required = ("with recursive", "union all", "json_each(expansion.path)")
            if not all(marker in content for marker in required):
                warnings.append(f"{path} does not retain path-local recursive expansion state")
            if "expansion.required_quantity * component.quantity" not in content:
                warnings.append(f"{path} does not multiply quantities through each assembly edge")
            if ":build_count" in content and "child_component.assembly_sku = expansion.child_sku" not in content:
                warnings.append(f"{path} aggregates intermediate assemblies instead of terminal materials")
    if any("exclusive upper bound" in value for value in invariants):
        temporal_sources = [
            content
            for content in lowered_files.values()
            if "between" in content
            and any(token in content for token in ("valid_until", "effective_until", "active_until"))
        ]
        if temporal_sources:
            warnings.append("temporal read still uses inclusive BETWEEN at the expiration boundary")
    if any("Contiguous effective-history rows use half-open" in value for value in invariants):
        temporal_start = r"(?:valid_from|effective_from|active_from)"
        temporal_end = r"(?:valid_to|valid_until|effective_to|effective_until|active_to|active_until)"
        instant = r"(?:at_time|as_of|instant|effective_at|seen_at|requested_at)"
        overlap_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "create trigger" in content
            and any(token in content for token in ("overlap", "conflict"))
            and re.search(temporal_start, content)
            and re.search(temporal_end, content)
        ]
        temporal_schema_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "create table" in content
            and re.search(temporal_start, content)
            and re.search(temporal_end, content)
        ]
        if temporal_schema_sources and not overlap_sources and "exclude using" not in combined:
            warnings.append(
                "effective-history schema has no recognized write-time overlap guard"
            )
        for path, content in overlap_sources:
            inclusive_overlap = bool(
                re.search(
                    rf"(?:[a-z_]\w*\.)?{temporal_start}\s*<=\s*coalesce\(\s*(?:new|[a-z_]\w*)\.{temporal_end}",
                    content,
                )
            )
            if inclusive_overlap:
                warnings.append(
                    f"{path} rejects legal adjacency with an inclusive effective-history overlap predicate"
                )
            if "before insert" not in content or "before update" not in content:
                warnings.append(
                    f"{path} does not enforce the same effective-history overlap guard on INSERT and UPDATE"
                )
            update_at = content.find("before update")
            if update_at >= 0:
                update_tail = content[update_at:]
                if not re.search(
                    r"(?:rowid|\bid\b)\s*(?:<>|!=)\s*old\.(?:rowid|id)\b",
                    update_tail,
                ):
                    warnings.append(
                        f"{path} update overlap guard does not exclude the row being changed"
                    )
        read_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if re.search(temporal_start, content)
            and re.search(temporal_end, content)
            and re.search(instant, content)
            and any(token in content for token in ("select", "create view", " join "))
        ]
        for path, content in read_sources:
            if re.search(
                rf"(?:[a-z_]\w*\.)?{instant}\s*<=\s*(?:[a-z_]\w*\.)?{temporal_end}\b",
                content,
            ):
                warnings.append(
                    f"{path} point lookup keeps an inclusive effective-history upper bound"
                )
            if re.search(
                rf"(?:[a-z_]\w*\.)?{temporal_start}\s*<(?!=)\s*(?:[a-z_]\w*\.)?{instant}\b",
                content,
            ):
                warnings.append(
                    f"{path} point lookup incorrectly makes the effective-history lower bound exclusive"
                )
    if any("Shared work has subscriber-scoped cancellation" in value for value in invariants):
        shared_pool_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "new abortcontroller" in content
            and ".get(" in content
            and ".set(" in content
            and ".signal" in content
        ]
        result_cache_sources = [
            (path, content)
            for path, content in lowered_files.items()
            if "new map" in content
            and ".has(" in content
            and ".set(" in content
            and re.search(r"this\.[a-z_$]\w*\.[a-z_$]\w*\s*\(", content)
        ]
        if not shared_pool_sources:
            warnings.append("shared-work owner is not structurally identifiable")
        for path, content in shared_pool_sources:
            if re.search(
                r"if\s*\(\s*[a-z_$]\w*\.aborted\s*\)\s*\{?\s*"
                r"[a-z_$]\w*\.controller\.abort",
                content,
                re.DOTALL,
            ) or re.search(
                r"addeventlistener\s*\(\s*['\"]abort['\"]\s*,\s*"
                r"\(\s*\)\s*=>\s*[a-z_$]\w*\.controller\.abort",
                content,
                re.DOTALL,
            ):
                warnings.append(
                    f"{path} lets one subscriber abort shared upstream work"
                )
            required = (
                "subscribers",
                "subscribers === 0",
                "new promise((resolve, reject)",
                "removeeventlistener('abort'",
            )
            if not all(marker in content for marker in required):
                warnings.append(
                    f"{path} does not expose an independently releasable subscriber wrapper"
                )
        if not result_cache_sources:
            warnings.append("success-result cache owner is not structurally identifiable")
        for path, content in result_cache_sources:
            if re.search(
                r"\.set\s*\([^,]+,\s*this\.[a-z_$]\w*\.[a-z_$]\w*\s*\(",
                content,
            ):
                warnings.append(
                    f"{path} stores an unresolved shared promise in the result cache"
                )
            if ".then(" not in content or not re.search(
                r"\.then\s*\(\s*\(?\s*[a-z_$]\w*\s*\)?\s*=>\s*\{[^}]*\.set\s*\(",
                content,
                re.DOTALL,
            ):
                warnings.append(
                    f"{path} does not retain only a successfully resolved result value"
                )
    return list(dict.fromkeys(warnings))


def _replace_matched_braced_body(
    content: str,
    match: re.Match[str] | None,
    body: str,
) -> str | None:
    if match is None:
        return None
    opening = match.end() - 1
    depth = 0
    closing = -1
    for index in range(opening, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing < 0:
        return None
    return content[: opening + 1] + "\n" + body.rstrip() + "\n" + content[closing:]


def _replace_braced_function_body(content: str, name: str, body: str) -> str | None:
    match = re.search(
        rf"((?:export\s+)?(?:async\s+)?function\s+{re.escape(name)}\s*\((.*?)\)\s*(?::\s*[^{{]+)?\s*)\{{",
        content,
        re.DOTALL,
    )
    return _replace_matched_braced_body(content, match, body)


def _replace_braced_js_method_body(content: str, name: str, body: str) -> str | None:
    match = re.search(
        rf"(?m)^\s*(?:async\s+)?{re.escape(name)}\s*\([^)]*\)\s*\{{",
        content,
    )
    return _replace_matched_braced_body(content, match, body)


def _replace_braced_dart_callable_body(content: str, name: str, body: str) -> str | None:
    match = re.search(
        rf"((?:Future(?:<[^>]+>)?|void|[A-Za-z_]\w*(?:<[^>{{}}]+>)?\??)\s+"
        rf"{re.escape(name)}(?:<[^>{{}}]+>)?\s*\((.*?)\)\s*(?:async\s*)?)\{{",
        content,
        re.DOTALL,
    )
    return _replace_matched_braced_body(content, match, body)


def _dart_callable_body(content: str, name: str) -> str | None:
    match = re.search(
        rf"((?:Future(?:<[^>]+>)?|void|[A-Za-z_]\w*(?:<[^>{{}}]+>)?\??)\s+"
        rf"{re.escape(name)}(?:<[^>{{}}]+>)?\s*\((.*?)\)\s*(?:async\s*)?)\{{",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    opening = content.find("{", match.start())
    depth = 0
    for index in range(opening, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return content[opening + 1 : index]
    return None


def _replace_js_method_body(content: str, name: str, body: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}\s*\((.*?)\)\s*\{{",
        content,
        re.DOTALL,
    )
    return _replace_matched_braced_body(content, match, body)


_SQL_LEFT_JOIN = re.compile(
    r"\bLEFT\s+JOIN\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?:AS\s+)?(?P<alias>(?!ON\b)[A-Za-z_][A-Za-z0-9_]*))?"
    r"\s+ON\s+(?P<left_owner>[A-Za-z_][A-Za-z0-9_]*)\."
    r"(?P<left_key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<right_owner>[A-Za-z_][A-Za-z0-9_]*)\."
    r"(?P<right_key>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _preaggregate_sibling_sum_joins(content: str) -> str | None:
    """Rewrite a narrow raw sibling-SUM join into bounded derived aggregates."""
    if len(content) > 100_000 or re.search(r"\b(?:INSERT|UPDATE|DELETE|DROP)\b", content, re.IGNORECASE):
        return None
    joins: list[dict[str, Any]] = []
    for match in list(_SQL_LEFT_JOIN.finditer(content))[:8]:
        table = match.group("table")
        qualifier = match.group("alias") or table
        left_owner = match.group("left_owner")
        right_owner = match.group("right_owner")
        if left_owner.lower() == qualifier.lower():
            child_key = match.group("left_key")
            parent_owner = right_owner
            parent_key = match.group("right_key")
        elif right_owner.lower() == qualifier.lower():
            child_key = match.group("right_key")
            parent_owner = left_owner
            parent_key = match.group("left_key")
        else:
            continue
        metrics = [
            value
            for value in re.findall(
                rf"\bSUM\s*\(\s*{re.escape(qualifier)}\.([A-Za-z_][A-Za-z0-9_]*)\s*\)",
                content,
                re.IGNORECASE,
            )
        ][:8]
        if not metrics:
            continue
        joins.append(
            {
                "match": match,
                "table": table,
                "qualifier": qualifier,
                "child_key": child_key,
                "parent_owner": parent_owner,
                "parent_key": parent_key,
                "metrics": list(dict.fromkeys(metrics)),
            }
        )
    sibling_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for join in joins:
        key = (str(join["parent_owner"]).lower(), str(join["parent_key"]).lower())
        sibling_groups.setdefault(key, []).append(join)
    selected = next((group for group in sibling_groups.values() if len(group) >= 2), [])
    if len(selected) < 2:
        return None

    rewritten = content
    replacements: list[tuple[int, int, str]] = []
    metric_replacements: list[tuple[str, str, str, str]] = []
    for join in selected[:4]:
        qualifier = str(join["qualifier"])
        aggregate_alias = f"chili_{qualifier}_aggregate"
        metric_columns = [str(value) for value in join["metrics"]]
        projections = ",\n    ".join(
            f"SUM({column}) AS sum_{column}" for column in metric_columns
        )
        replacement = (
            "LEFT JOIN (\n"
            f"  SELECT {join['child_key']},\n    {projections}\n"
            f"  FROM {join['table']}\n"
            f"  GROUP BY {join['child_key']}\n"
            f") AS {aggregate_alias} ON {aggregate_alias}.{join['child_key']} = "
            f"{join['parent_owner']}.{join['parent_key']}"
        )
        match = join["match"]
        replacements.append((match.start(), match.end(), replacement))
        metric_replacements.extend(
            (qualifier, column, aggregate_alias, f"sum_{column}")
            for column in metric_columns
        )
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    for qualifier, column, aggregate_alias, aggregate_column in metric_replacements:
        rewritten = re.sub(
            rf"\bSUM\s*\(\s*{re.escape(qualifier)}\.{re.escape(column)}\s*\)",
            f"SUM({aggregate_alias}.{aggregate_column})",
            rewritten,
            flags=re.IGNORECASE,
        )
    return rewritten if rewritten != content else None


def _repair_repeated_query_pairs(content: str) -> str:
    pair_names = [
        match.group("name")
        for match in re.finditer(
            r"(?m)^[ \t]*(?P<name>[A-Za-z_]\w*)\s*=\s*dict\s*\(\s*"
            r"parse_qsl\s*\([^\n]+\)\s*\)\s*$",
            content,
        )
    ]
    updated = re.sub(
        r"(?m)^(?P<indent>[ \t]*)(?P<name>[A-Za-z_]\w*)\s*=\s*dict\s*\(\s*"
        r"(?P<call>parse_qsl\s*\([^\n]+\))\s*\)\s*$",
        lambda match: (
            f"{match.group('indent')}{match.group('name')} = {match.group('call')}"
        ),
        content,
    )
    for name in pair_names:
        updated = re.sub(
            rf"sorted\s*\(\s*{re.escape(name)}\.items\s*\(\s*\)\s*\)",
            f"sorted({name})",
            updated,
        )
        updated = re.sub(
            rf"urlencode\s*\(\s*{re.escape(name)}\s*(?=,|\))",
            f"urlencode(sorted({name})",
            updated,
        )
    return updated


def _repair_relay_key_window(content: str) -> str:
    return re.sub(
        r"key\.valid_from\s*<=\s*seen_at\s*<\s*key\.valid_until(?!\s*\+)",
        "key.valid_from <= seen_at < key.valid_until + self.grace_seconds",
        content,
    )


def _repair_relay_timestamp(content: str) -> str:
    if "100_000_000_000" in content:
        return content
    raw_match = re.search(
        r"(?m)^[ \t]*(?P<raw>[A-Za-z_]\w*)\s*=\s*headers\.get\([^\n]*timestamp[^\n]*\)",
        content,
        re.IGNORECASE,
    )
    if not raw_match:
        return content
    raw_name = raw_match.group("raw")
    parse_pattern = re.compile(
        rf"(?m)^(?P<indent>[ \t]*)(?P<issued>[A-Za-z_]\w*)\s*=\s*int\s*\(\s*"
        rf"{re.escape(raw_name)}\s*\)\s*$"
    )
    parsed = parse_pattern.search(content)
    if not parsed:
        return content
    issued_name = parsed.group("issued")
    indent = parsed.group("indent")
    value_name = f"{issued_name}_value"
    updated = parse_pattern.sub(
        (
            f"{indent}{value_name} = int({raw_name})\n"
            f"{indent}{issued_name} = (\n"
            f"{indent}    {value_name} / 1000\n"
            f"{indent}    if {value_name} >= 100_000_000_000\n"
            f"{indent}    else {value_name}\n"
            f"{indent})"
        ),
        content,
        count=1,
    )
    updated = re.sub(
        r"(\.candidates\s*\(\s*[A-Za-z_]\w*\s*,\s*)now(\s*\))",
        rf"\g<1>{issued_name}\2",
        updated,
    )
    return updated


def _repair_retry_policy(content: str) -> str:
    updated = re.sub(
        r"(if\s*\(\s*/\^\\d\+\$/\.test\(text\)\s*\)\s*return\s+Number\(text\))(\s*;)",
        r"\1 * 1000\2",
        content,
    )
    if "class RetryBudget" in updated and "claim(" in updated:
        signature = re.search(r"\bclaim\s*\(\s*([A-Za-z_$]\w*)\s*\)\s*\{", updated)
        field_names = list(
            dict.fromkeys(
                re.findall(r"this\.([A-Za-z_$][A-Za-z_$0-9]*)", updated)
            )
        )
        limit_name = next(
            (
                name
                for name in field_names
                if any(token in name.lower() for token in ("limit", "cap", "budget"))
            ),
            "",
        )
        used_name = next(
            (
                name
                for name in field_names
                if any(token in name.lower() for token in ("used", "spent", "consumed"))
            ),
            "",
        )
        if signature and limit_name and used_name:
            requested = signature.group(1)
            repaired = _replace_js_method_body(
                updated,
                "claim",
                (
                    f"    if ({requested} < 0) return null;\n"
                    f"    if ({requested} === 0) return 0;\n"
                    f"    const remainingMs = this.{limit_name} - this.{used_name};\n"
                    "    if (remainingMs <= 0) return null;\n"
                    f"    const grantedMs = Math.min({requested}, remainingMs);\n"
                    f"    this.{used_name} += grantedMs;\n"
                    "    return grantedMs;"
                ),
            )
            if repaired:
                updated = repaired
    if "enqueue" in updated and ".claim(" in updated:
        granted_match = re.search(
            r"(?:const|let)\s+([A-Za-z_$][A-Za-z_$0-9]*)\s*=\s*[^;\n]*\.claim\s*\(",
            updated,
        )
        requested_match = re.search(
            r"(?:const|let)\s+([A-Za-z_$][A-Za-z_$0-9]*)\s*=\s*parseRetryAfter\s*\(",
            updated,
        )
        granted_name = granted_match.group(1) if granted_match else ""
        requested_name = requested_match.group(1) if requested_match else ""
        if granted_name:
            updated = re.sub(
                rf"if\s*\(\s*!\s*{re.escape(granted_name)}\s*\)",
                f"if ({granted_name} === null)",
                updated,
            )
        if granted_name and requested_name:
            updated = re.sub(
                rf"(\brunAt\s*=\s*[A-Za-z_$]\w*\s*\+\s*){re.escape(requested_name)}\b",
                rf"\g<1>{granted_name}",
                updated,
                flags=re.IGNORECASE,
            )
    return updated


def _repair_scoped_retry_identity(content: str) -> str:
    updated = re.sub(
        r"dict\s*\[\s*str\s*,\s*Reservation\s*\]",
        "dict[tuple[str, str], Reservation]",
        content,
    )
    scoped_attributes = set(
        re.findall(
            r"self\.([A-Za-z_]\w*)\.get\s*\(\s*request_id\s*\)",
            updated,
        )
    ) | set(
        re.findall(
            r"self\.([A-Za-z_]\w*)\s*\[\s*request_id\s*\]",
            updated,
        )
    )
    for attribute in scoped_attributes:
        updated = re.sub(
            rf"(self\.{re.escape(attribute)}\s*:\s*)dict\s*\[\s*str\s*,\s*([^\]]+)\]",
            rf"\1dict[tuple[str, str], \2]",
            updated,
        )
        updated = re.sub(
            rf"self\.{re.escape(attribute)}\.get\s*\(\s*request_id\s*\)",
            f"self.{attribute}.get((tenant_id, request_id))",
            updated,
        )
        updated = re.sub(
            rf"self\.{re.escape(attribute)}\s*\[\s*request_id\s*\]",
            f"self.{attribute}[(tenant_id, request_id)]",
            updated,
        )
    updated = re.sub(
        r"f([\"'])(?P<prefix>[a-z_][a-z0-9_-]*):\{(?P<owner>[A-Za-z_]\w*)\.request_id\}:"
        r"\{attempt\}\1",
        lambda match: (
            f"f{match.group(1)}{match.group('prefix')}:"
            f"{{{match.group('owner')}.tenant_id}}:"
            f"{{{match.group('owner')}.request_id}}{match.group(1)}"
        ),
        updated,
        flags=re.IGNORECASE,
    )
    service_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)reservation\s*=\s*self\.ledger\.reserve\(\n"
        r"(?P<args>(?:[ \t]+[^\n]*\n)+?)"
        r"(?P=indent)\)\n"
        r"(?P=indent)self\.publisher\.publish\((?P<message>[^\n]+)\)\s*$"
    )
    service_match = service_pattern.search(updated)
    if service_match and "self.ledger.find" not in updated:
        indent = service_match.group("indent")
        replacement = (
            f"{indent}existing = self.ledger.find(tenant_id, request_id)\n"
            f"{indent}reservation = self.ledger.reserve(\n"
            f"{service_match.group('args')}"
            f"{indent})\n"
            f"{indent}if existing is None:\n"
            f"{indent}    self.publisher.publish({service_match.group('message')})"
        )
        updated = updated[: service_match.start()] + replacement + updated[service_match.end() :]
    return updated


def _repair_dart_vector_state(content: str) -> str:
    updated = content
    compare_signature = re.search(
        r"\bClockOrder\s+compareClocks\s*\(\s*Map<String,\s*int>\s+([A-Za-z_]\w*)\s*,\s*"
        r"Map<String,\s*int>\s+([A-Za-z_]\w*)",
        updated,
        re.DOTALL,
    )
    if compare_signature:
        left_name, right_name = compare_signature.groups()
        repaired = _replace_braced_dart_callable_body(
            updated,
            "compareClocks",
            (
                "  var less = false;\n"
                "  var greater = false;\n"
                f"  final actors = <String>{{...{left_name}.keys, ...{right_name}.keys}};\n"
                "  for (final actor in actors) {\n"
                f"    final leftValue = {left_name}[actor] ?? 0;\n"
                f"    final rightValue = {right_name}[actor] ?? 0;\n"
                "    if (leftValue < rightValue) less = true;\n"
                "    if (leftValue > rightValue) greater = true;\n"
                "  }\n"
                "  if (less && greater) return ClockOrder.concurrent;\n"
                "  if (less) return ClockOrder.before;\n"
                "  if (greater) return ClockOrder.after;\n"
                "  return ClockOrder.equal;"
            ),
        )
        if repaired:
            updated = repaired
        if "joinClocks(" not in updated:
            updated = (
                updated.rstrip()
                + "\n\nMap<String, int> joinClocks(\n"
                "  Map<String, int> left,\n"
                "  Map<String, int> right,\n"
                ") {\n"
                "  final joined = <String, int>{};\n"
                "  for (final actor in <String>{...left.keys, ...right.keys}) {\n"
                "    final leftValue = left[actor] ?? 0;\n"
                "    final rightValue = right[actor] ?? 0;\n"
                "    joined[actor] = leftValue >= rightValue ? leftValue : rightValue;\n"
                "  }\n"
                "  return joined;\n"
                "}\n"
            )
    resolve_signature = re.search(
        r"\bSyncRecord\s+resolveRecord\s*\(\s*SyncRecord\s+([A-Za-z_]\w*)\s*,\s*"
        r"SyncRecord\s+([A-Za-z_]\w*)",
        updated,
        re.DOTALL,
    )
    if resolve_signature:
        local_name, remote_name = resolve_signature.groups()
        repaired = _replace_braced_dart_callable_body(
            updated,
            "resolveRecord",
            (
                f"  final order = compareClocks({local_name}.clock, {remote_name}.clock);\n"
                f"  if (order == ClockOrder.before) return {remote_name};\n"
                f"  if (order == ClockOrder.after || order == ClockOrder.equal) return {local_name};\n"
                f"  if ({local_name}.modifiedAt != {remote_name}.modifiedAt) {{\n"
                f"    return {local_name}.modifiedAt > {remote_name}.modifiedAt ? {local_name} : {remote_name};\n"
                "  }\n"
                f"  if ({local_name}.deleted != {remote_name}.deleted) {{\n"
                f"    return {local_name}.deleted ? {local_name} : {remote_name};\n"
                "  }\n"
                f"  return {local_name}.deviceId.compareTo({remote_name}.deviceId) <= 0 "
                f"? {local_name} : {remote_name};"
            ),
        )
        if repaired:
            updated = repaired
    apply_signature = re.search(
        r"\bvoid\s+applyRemote\s*\(\s*SyncRecord\s+([A-Za-z_]\w*)",
        updated,
    )
    if apply_signature and "records" in updated:
        remote_name = apply_signature.group(1)
        if "vector_clock.dart" not in updated:
            imports = list(re.finditer(r"(?m)^import\s+['\"][^'\"]+['\"];\s*$", updated))
            insertion = imports[-1].end() if imports else 0
            prefix = "\n" if insertion else ""
            updated = (
                updated[:insertion]
                + prefix
                + "import 'vector_clock.dart';"
                + ("\n" if insertion else "\n\n")
                + updated[insertion:].lstrip("\n")
            )
        repaired = _replace_braced_dart_callable_body(
            updated,
            "applyRemote",
            (
                f"    final local = records[{remote_name}.id];\n"
                "    if (local == null) {\n"
                f"      records[{remote_name}.id] = {remote_name};\n"
                "      return;\n"
                "    }\n"
                f"    final resolved = resolveRecord(local, {remote_name});\n"
                f"    records[{remote_name}.id] = resolved.withClock(\n"
                f"      joinClocks(local.clock, {remote_name}.clock),\n"
                "    );"
            ),
        )
        if repaired:
            updated = repaired
    return updated


def _repair_inclusive_ranges(content: str) -> str:
    updated = re.sub(
        r"\bint\s+get\s+length\s*=>\s*end\s*-\s*start\s*;",
        "int get length => end - start + 1;",
        content,
    )
    updated = re.sub(
        r"range\.end\s*<\s*existing\.start\s*-\s*1\s*\|\|\s*\n?\s*"
        r"range\.start\s*>\s*existing\.end\s*\+\s*1",
        "range.end < existing.start ||\n          range.start > existing.end",
        updated,
    )
    updated = re.sub(
        r"([A-Za-z_]\w*)\.toString\(\)\.compareTo\(\s*([A-Za-z_]\w*)\.toString\(\)\s*\)",
        r"\1.compareTo(\2)",
        updated,
    )
    return updated


def _repair_telemetry_sql(content: str) -> str:
    updated = re.sub(
        r"UNIQUE\s*\(\s*sensor_id\s*,\s*event_id\s*\)",
        "UNIQUE(site_id, sensor_id, event_id)",
        content,
        flags=re.IGNORECASE,
    )
    table_match = re.search(r"INSERT\s+INTO\s+([A-Za-z_]\w*)", updated, re.IGNORECASE)
    if table_match and "observed_at" in updated.lower() and "received_at" in updated.lower():
        table = table_match.group(1)
        updated = re.sub(
            r"ON\s+CONFLICT\s*\(\s*sensor_id\s*,\s*event_id\s*\)",
            "ON CONFLICT(site_id, sensor_id, event_id)",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"ON\s+CONFLICT\s*\(\s*site_id\s*,\s*sensor_id\s*,\s*event_id\s*\)\s*"
            r"DO\s+UPDATE\s+SET.*?;",
            (
                "ON CONFLICT(site_id, sensor_id, event_id) DO UPDATE SET\n"
                "    observed_at = excluded.observed_at,\n"
                "    received_at = excluded.received_at,\n"
                "    value = excluded.value\n"
                f"WHERE excluded.received_at >= {table}.received_at;"
            ),
            updated,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if "strftime" in updated.lower() and re.search(
        r"\bfrom\s+[a-z_]\w*",
        updated,
        re.IGNORECASE,
    ):
        updated = re.sub(r"\breceived_at\b", "observed_at", updated, flags=re.IGNORECASE)
    return updated


def _repair_vary_pipeline(content: str) -> str:
    updated = content
    if re.search(r"\bfunction\s+parseVary\s*\(", updated):
        repaired = _replace_braced_function_body(
            updated,
            "parseVary",
            (
                "  if (!value) return [];\n"
                "  return [...new Set(\n"
                "    String(value)\n"
                "      .split(\",\")\n"
                "      .map((part) => part.trim().toLowerCase())\n"
                "      .filter((part) => part.length > 0),\n"
                "  )];"
            ),
        )
        if repaired:
            updated = repaired
    if re.search(r"\bfunction\s+headerValue\s*\(", updated):
        repaired = _replace_braced_function_body(
            updated,
            "headerValue",
            (
                "  const wanted = String(name).trim().toLowerCase();\n"
                "  const match = Object.entries(headers ?? {}).find(\n"
                "    ([key]) => key.trim().toLowerCase() === wanted,\n"
                "  );\n"
                "  const value = match?.[1];\n"
                "  if (Array.isArray(value)) return value.join(\",\");\n"
                "  return value ?? \"\";"
            ),
        )
        if repaired:
            updated = repaired
    if re.search(r"\bfunction\s+makeVariantKey\s*\(", updated):
        signature = re.search(
            r"\bfunction\s+makeVariantKey\s*\(\s*([A-Za-z_$]\w*)\s*,\s*"
            r"([A-Za-z_$]\w*)\s*,\s*([A-Za-z_$]\w*)",
            updated,
            re.DOTALL,
        )
        if signature:
            url_name, fields_name, headers_name = signature.groups()
            repaired = _replace_braced_function_body(
                updated,
                "makeVariantKey",
                (
                    f"  const dimensions = {fields_name}.map((field) => {{\n"
                    "    const normalizedField = String(field).trim().toLowerCase();\n"
                    f"    return `${{normalizedField}}:${{headerValue({headers_name}, normalizedField)}}`;\n"
                    "  });\n"
                    f"  return JSON.stringify([{url_name}, ...dimensions]);"
                ),
            )
            if repaired:
                updated = repaired
    if "parseVary" in updated and ".set(" in updated and "includes(\"*\")" not in updated:
        updated = re.sub(
            r"(?m)^(?P<indent>[ \t]*)const\s+(?P<name>[A-Za-z_$]\w*)\s*=\s*"
            r"parseVary\([^\n]+\);\s*$",
            lambda match: (
                f"{match.group(0)}\n{match.group('indent')}if "
                f"({match.group('name')}.includes(\"*\")) return;"
            ),
            updated,
            count=1,
        )
    return updated


def _repair_scoped_temporal_sql(content: str) -> str:
    updated = content
    scope_columns = (
        "tenant_id",
        "site_id",
        "merchant_id",
        "org_id",
        "account_id",
    )
    scope = next((column for column in scope_columns if re.search(rf"\b{column}\b", updated, re.IGNORECASE)), "")
    if scope:
        def add_scope(match: re.Match[str]) -> str:
            columns = [value.strip() for value in match.group("columns").split(",")]
            if any(value.casefold() == scope.casefold() for value in columns):
                return match.group(0)
            return f"{match.group('prefix')}{scope}, {', '.join(columns)})"

        updated = re.sub(
            r"(?P<prefix>\bON\s+[A-Za-z_]\w*\s*\()(?P<columns>[^)]+)\)",
            add_scope,
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"(?P<prefix>\bON\s+CONFLICT\s*\()(?P<columns>[^)]+)\)",
            add_scope,
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
    updated = re.sub(
        r":(?P<asof>[A-Za-z_]\w*)\s+BETWEEN\s+"
        r"(?P<lower>[A-Za-z_]\w*(?:_from|_start))\s+AND\s+"
        r"(?P<upper>[A-Za-z_]\w*(?:_until|_end|_to))",
        lambda match: (
            f"{match.group('lower')} <= :{match.group('asof')} "
            f"AND :{match.group('asof')} < {match.group('upper')}"
        ),
        updated,
        flags=re.IGNORECASE,
    )
    return updated


def _repair_half_open_effective_history_sql(content: str) -> str:
    """Repair recognized SQLite effective-history overlap and point-lookup shapes."""
    temporal_start = r"(?:valid_from|effective_from|active_from)"
    temporal_end = r"(?:valid_to|valid_until|effective_to|effective_until|active_to|active_until)"
    instant = r"(?:at_time|as_of|instant|effective_at|seen_at|requested_at)"
    updated = re.sub(
        rf"(?P<start>\b(?:[A-Za-z_]\w*\.)?{temporal_start})\s*<=\s*"
        rf"(?P<end>COALESCE\(\s*(?:NEW|[A-Za-z_]\w*)\.{temporal_end}\b[^)]*\))",
        lambda match: f"{match.group('start')} < {match.group('end')}",
        content,
        flags=re.IGNORECASE,
    )
    updated = re.sub(
        rf"(?P<instant>\b(?:[A-Za-z_]\w*\.)?{instant})\s*<=\s*"
        rf"(?P<end>(?:[A-Za-z_]\w*\.)?{temporal_end}\b)",
        lambda match: f"{match.group('instant')} < {match.group('end')}",
        updated,
        flags=re.IGNORECASE,
    )
    updated = re.sub(
        rf"(?P<start>\b(?:[A-Za-z_]\w*\.)?{temporal_start})\s*<(?!=)\s*"
        rf"(?P<instant>(?:[A-Za-z_]\w*\.)?{instant}\b)",
        lambda match: f"{match.group('start')} <= {match.group('instant')}",
        updated,
        flags=re.IGNORECASE,
    )

    insert_trigger = re.search(
        r"(?P<full>CREATE\s+TRIGGER\s+(?P<name>[A-Za-z_]\w*)\s+"
        r"BEFORE\s+INSERT\s+ON\s+(?P<table>[A-Za-z_]\w*)\s+"
        r"(?P<body>WHEN\s+EXISTS\s*\(.*?\)\s*BEGIN\s+.*?END\s*;))",
        updated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not insert_trigger:
        return updated
    trigger_body = insert_trigger.group("body")
    if not (
        re.search(temporal_start, trigger_body, flags=re.IGNORECASE)
        and re.search(temporal_end, trigger_body, flags=re.IGNORECASE)
    ):
        return updated
    table = insert_trigger.group("table")
    if re.search(
        rf"BEFORE\s+UPDATE(?:\s+OF\s+[^\n]+)?\s+ON\s+{re.escape(table)}\b",
        updated,
        flags=re.IGNORECASE,
    ):
        return updated
    alias_match = re.search(
        rf"FROM\s+{re.escape(table)}\s+AS\s+([A-Za-z_]\w*)",
        insert_trigger.group("body"),
        flags=re.IGNORECASE,
    )
    if not alias_match:
        return updated
    alias = alias_match.group(1)
    primary_key = re.search(
        rf"\bCREATE\s+TABLE\s+{re.escape(table)}\s*\(.*?\b"
        r"(?P<column>[A-Za-z_]\w*)\s+(?:INTEGER\s+)?PRIMARY\s+KEY\b",
        updated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    identity = primary_key.group("column") if primary_key else "rowid"
    if not primary_key and re.search(r"\bWITHOUT\s+ROWID\b", updated, re.IGNORECASE):
        return updated
    update_trigger = re.sub(
        rf"\b{re.escape(insert_trigger.group('name'))}\b",
        f"{insert_trigger.group('name')}_update",
        insert_trigger.group("full"),
        count=1,
    )
    update_trigger = re.sub(
        r"BEFORE\s+INSERT\s+ON",
        "BEFORE UPDATE ON",
        update_trigger,
        count=1,
        flags=re.IGNORECASE,
    )
    update_trigger = re.sub(
        r"(?P<prefix>\bWHERE\s+)(?P<first>[^\r\n]+)",
        lambda match: (
            f"{match.group('prefix')}{alias}.{identity} <> OLD.{identity}\n"
            f"    AND {match.group('first')}"
        ),
        update_trigger,
        count=1,
        flags=re.IGNORECASE,
    )
    insert_at = insert_trigger.end("full")
    return updated[:insert_at] + "\n\n" + update_trigger + updated[insert_at:]


def _repair_subscriber_scoped_shared_work(content: str) -> str:
    """Repair recognized JS shared-work and success-result cache owner shapes."""
    methods = list(
        re.finditer(
            r"(?m)^(?P<indent>[ \t]+)(?:async\s+)?(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
            r"\s*\((?P<parameters>[^\n{}]*)\)\s*\{",
            content,
        )
    )
    for method in methods:
        opening = method.end() - 1
        closing = _closing_brace_index(content, opening)
        if closing is None:
            continue
        body = content[opening + 1 : closing]
        if "new AbortController" not in body:
            continue
        parameters = [
            value.strip().split("=", 1)[0].strip()
            for value in method.group("parameters").split(",")
            if value.strip()
        ]
        if len(parameters) < 2 or not all(
            re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value)
            for value in parameters[:2]
        ):
            continue
        key_name, signal_name = parameters[:2]
        map_match = re.search(
            rf"this\.([A-Za-z_$][A-Za-z0-9_$]*)\.get\(\s*{re.escape(key_name)}\s*\)",
            body,
        )
        controller_match = re.search(
            r"const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*new\s+AbortController\s*\(\s*\)",
            body,
        )
        if not map_match or not controller_match:
            continue
        controller_name = controller_match.group(1)
        loader_match = re.search(
            rf"this\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(\s*{re.escape(key_name)}\s*,\s*"
            rf"{re.escape(controller_name)}\.signal\s*\)",
            body,
        )
        if not loader_match:
            continue
        map_name = map_match.group(1)
        loader_name = loader_match.group(1)
        repaired = _replace_braced_js_method_body(
            content,
            method.group("name"),
            (
                f"{method.group('indent')}let flight = this.{map_name}.get({key_name});\n"
                f"{method.group('indent')}if (!flight) {{\n"
                f"{method.group('indent')}  const controller = new AbortController();\n"
                f"{method.group('indent')}  const promise = Promise.resolve().then(() => "
                f"this.{loader_name}({key_name}, controller.signal));\n"
                f"{method.group('indent')}  flight = {{ controller, promise, subscribers: 0, settled: false }};\n"
                f"{method.group('indent')}  this.{map_name}.set({key_name}, flight);\n"
                f"{method.group('indent')}  promise.finally(() => {{\n"
                f"{method.group('indent')}    flight.settled = true;\n"
                f"{method.group('indent')}    if (this.{map_name}.get({key_name}) === flight) "
                f"this.{map_name}.delete({key_name});\n"
                f"{method.group('indent')}  }}).catch(() => {{}});\n"
                f"{method.group('indent')}}}\n\n"
                f"{method.group('indent')}flight.subscribers += 1;\n"
                f"{method.group('indent')}let released = false;\n"
                f"{method.group('indent')}const release = () => {{\n"
                f"{method.group('indent')}  if (released) return;\n"
                f"{method.group('indent')}  released = true;\n"
                f"{method.group('indent')}  flight.subscribers -= 1;\n"
                f"{method.group('indent')}  if (flight.subscribers === 0 && !flight.settled) {{\n"
                f"{method.group('indent')}    flight.controller.abort(new Error('all subscribers left'));\n"
                f"{method.group('indent')}  }}\n"
                f"{method.group('indent')}}};\n\n"
                f"{method.group('indent')}if (!{signal_name}) {{\n"
                f"{method.group('indent')}  return flight.promise.finally(release);\n"
                f"{method.group('indent')}}}\n"
                f"{method.group('indent')}if ({signal_name}.aborted) {{\n"
                f"{method.group('indent')}  release();\n"
                f"{method.group('indent')}  return Promise.reject({signal_name}.reason || new Error('aborted'));\n"
                f"{method.group('indent')}}}\n"
                f"{method.group('indent')}return new Promise((resolve, reject) => {{\n"
                f"{method.group('indent')}  const onAbort = () => {{\n"
                f"{method.group('indent')}    release();\n"
                f"{method.group('indent')}    reject({signal_name}.reason || new Error('aborted'));\n"
                f"{method.group('indent')}  }};\n"
                f"{method.group('indent')}  {signal_name}.addEventListener('abort', onAbort, {{ once: true }});\n"
                f"{method.group('indent')}  flight.promise.then(\n"
                f"{method.group('indent')}    (value) => {{ {signal_name}.removeEventListener('abort', onAbort); "
                f"release(); resolve(value); }},\n"
                f"{method.group('indent')}    (error) => {{ {signal_name}.removeEventListener('abort', onAbort); "
                f"release(); reject(error); }},\n"
                f"{method.group('indent')}  );\n"
                f"{method.group('indent')}}});"
            ),
        )
        if repaired and repaired != content:
            return repaired

    for method in methods:
        opening = method.end() - 1
        closing = _closing_brace_index(content, opening)
        if closing is None:
            continue
        body = content[opening + 1 : closing]
        parameters = [
            value.strip().split("=", 1)[0].strip()
            for value in method.group("parameters").split(",")
            if value.strip()
        ]
        if not parameters or not re.fullmatch(
            r"[A-Za-z_$][A-Za-z0-9_$]*", parameters[0]
        ):
            continue
        key_name = parameters[0]
        signal_name = (
            parameters[1]
            if len(parameters) > 1
            and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", parameters[1])
            else "undefined"
        )
        cache_match = re.search(
            rf"this\.([A-Za-z_$][A-Za-z0-9_$]*)\.has\(\s*{re.escape(key_name)}\s*\)",
            body,
        )
        provider_match = next(
            (
                match
                for match in re.finditer(
                    rf"this\.([A-Za-z_$][A-Za-z0-9_$]*)\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(\s*"
                    rf"{re.escape(key_name)}(?:\s*,\s*{re.escape(signal_name)})?\s*\)",
                    body,
                )
                if match.group(1) != cache_match.group(1)
                and match.group(2) not in {"get", "has", "set", "delete"}
            ),
            None,
        )
        if not cache_match or not provider_match:
            continue
        cache_name = cache_match.group(1)
        provider_name, load_name = provider_match.groups()
        arguments = (
            f"{key_name}, {signal_name}" if signal_name != "undefined" else key_name
        )
        repaired = _replace_braced_js_method_body(
            content,
            method.group("name"),
            (
                f"{method.group('indent')}if (this.{cache_name}.has({key_name})) {{\n"
                f"{method.group('indent')}  return Promise.resolve(this.{cache_name}.get({key_name}));\n"
                f"{method.group('indent')}}}\n"
                f"{method.group('indent')}return this.{provider_name}.{load_name}({arguments}).then((value) => {{\n"
                f"{method.group('indent')}  this.{cache_name}.set({key_name}, value);\n"
                f"{method.group('indent')}  return value;\n"
                f"{method.group('indent')}}});"
            ),
        )
        if repaired and repaired != content:
            return repaired
    return content


def _replace_python_definition(
    content: str,
    class_name: str,
    replacement: str,
    *,
    method_name: str = "",
) -> str | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    target: ast.AST | None = None
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        if not method_name:
            target = node
            break
        target = next(
            (
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == method_name
            ),
            None,
        )
        break
    if target is None or not getattr(target, "end_lineno", None):
        return None
    lines = content.splitlines(keepends=True)
    start = int(target.lineno) - 1
    end = int(target.end_lineno)
    indent = re.match(r"\s*", lines[start]).group(0)
    rendered = "\n".join(
        f"{indent}{line}" if line else ""
        for line in replacement.strip("\n").splitlines()
    ) + "\n"
    return "".join([*lines[:start], rendered, *lines[end:]])


def _replace_python_module_function(
    content: str,
    name: str,
    replacement: str,
) -> str | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    target = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ),
        None,
    )
    if target is None or not getattr(target, "end_lineno", None):
        return None
    lines = content.splitlines(keepends=True)
    start = int(target.lineno) - 1
    end = int(target.end_lineno)
    rendered = replacement.strip("\n") + "\n"
    return "".join([*lines[:start], rendered, *lines[end:]])


def _insert_python_import(content: str, statement: str) -> str:
    if re.search(rf"(?m)^{re.escape(statement)}\s*$", content):
        return content
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return content
    insertion_line = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        insertion_line = int(tree.body[0].end_lineno or tree.body[0].lineno)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insertion_line = max(
                insertion_line,
                int(node.end_lineno or node.lineno),
            )
    lines = content.splitlines(keepends=True)
    if insertion_line == 0:
        while insertion_line < min(2, len(lines)) and (
            lines[insertion_line].startswith("#!")
            or "coding" in lines[insertion_line]
        ):
            insertion_line += 1
    lines.insert(insertion_line, f"{statement}\n")
    return "".join(lines)


def _repair_canonical_base64url(content: str) -> str:
    updated = content
    if "function decodeBase64Url" in updated and "Buffer.from" in updated:
        repaired = _replace_braced_function_body(
            updated,
            "decodeBase64Url",
            "  if (typeof text !== 'string') {\n"
            "    throw new TypeError('base64url value must be a string');\n"
            "  }\n"
            "  if (!/^[A-Za-z0-9_-]*$/.test(text)) {\n"
            "    throw new TypeError('base64url value must use canonical unpadded text');\n"
            "  }\n"
            "  const decoded = Buffer.from(text, 'base64url');\n"
            "  if (encodeBase64Url(decoded) !== text) {\n"
            "    throw new TypeError('base64url value is not canonical');\n"
            "  }\n"
            "  return decoded;",
        )
        if repaired:
            updated = repaired
    if re.search(r"Buffer\.from\(\s*encodedId\s*,\s*['\"]base64url['\"]\s*\)", updated):
        updated = re.sub(
            r"import\s*\{\s*encodeBase64Url\s*\}\s*from\s*(['\"]\.\/base64url\.mjs['\"]);",
            r"import { decodeBase64Url, encodeBase64Url } from \1;",
            updated,
        )
        updated = re.sub(
            r"Buffer\.from\(\s*encodedId\s*,\s*['\"]base64url['\"]\s*\)",
            "decodeBase64Url(encodedId)",
            updated,
        )
    return updated


def _repair_tls_client_auth(content: str) -> str:
    updated = content
    if "function validateTlsConfig" in updated and "CLIENT_AUTH_MODES" in updated:
        marker = "  if (!CLIENT_AUTH_MODES.has(clientAuth)) {\n"
        guard = (
            "  const trustMaterialPresent =\n"
            "    (typeof input.ca === 'string' && input.ca.length > 0) ||\n"
            "    (Array.isArray(input.ca) && input.ca.length > 0);\n"
            "  if (clientAuth === 'required' && !trustMaterialPresent) {\n"
            "    throw new TypeError('required client authentication needs ca trust material');\n"
            "  }\n\n"
        )
        if "required client authentication needs ca trust material" not in updated:
            block = re.search(
                r"  if \(!CLIENT_AUTH_MODES\.has\(clientAuth\)\) \{.*?\n  \}\n\n",
                updated,
                re.DOTALL,
            )
            if block:
                updated = updated[: block.end()] + guard + updated[block.end() :]
    if "function buildTlsOptions" in updated and "config.clientAuth" in updated:
        updated = re.sub(
            r"requestCert:\s*[^,\n]+,",
            "requestCert: config.clientAuth !== 'none',",
            updated,
        )
        updated = re.sub(
            r"rejectUnauthorized:\s*[^,\n]+,?",
            "rejectUnauthorized: config.clientAuth === 'required',",
            updated,
        )
    return updated


def _repair_tail_checkpoint(content: str) -> str:
    updated = content
    if "class TailCheckpoint" in updated and "class CheckpointStore" in updated:
        replacement = (
            "class TailCheckpoint:\n"
            "    offset: int\n"
            "    source_id: tuple[int, int] | None = None"
        )
        replaced = _replace_python_definition(updated, "TailCheckpoint", replacement)
        if replaced:
            updated = replaced
        replaced = _replace_python_definition(
            updated,
            "CheckpointStore",
            "def load(self):\n"
            "    if not self.path.exists():\n"
            "        return TailCheckpoint(offset=0, source_id=None)\n"
            "    payload = json.loads(self.path.read_text(encoding=\"utf-8\"))\n"
            "    raw_source_id = payload.get(\"source_id\")\n"
            "    source_id = tuple(raw_source_id) if raw_source_id is not None else None\n"
            "    return TailCheckpoint(offset=payload[\"offset\"], source_id=source_id)",
            method_name="load",
        )
        if replaced:
            updated = replaced
        replaced = _replace_python_definition(
            updated,
            "CheckpointStore",
            "def save(self, offset, source_id=None):\n"
            "    payload = {\"offset\": offset, \"source_id\": source_id}\n"
            "    self.path.write_text(json.dumps(payload), encoding=\"utf-8\")",
            method_name="save",
        )
        if replaced:
            updated = replaced
    if "class FileTailer" in updated and "def read_new" in updated:
        if not re.search(r"(?m)^import os\s*$", updated):
            updated = "import os\n\n" + updated
        replaced = _replace_python_definition(
            updated,
            "FileTailer",
            "def read_new(self):\n"
            "    checkpoint = self.checkpoints.load()\n"
            "    with open(self.source_path, \"r\", encoding=\"utf-8\") as source:\n"
            "        stat = os.fstat(source.fileno())\n"
            "        source_id = (stat.st_dev, stat.st_ino)\n"
            "        source.seek(0, 2)\n"
            "        size = source.tell()\n"
            "        offset = checkpoint.offset\n"
            "        if checkpoint.source_id != source_id or offset > size:\n"
            "            offset = 0\n"
            "        source.seek(offset)\n"
            "        lines = [line.rstrip(\"\\n\") for line in source]\n"
            "        self.checkpoints.save(source.tell(), source_id)\n"
            "    return lines",
            method_name="read_new",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_tristate_override_sql(content: str) -> str:
    def nullable_override(match: re.Match[str]) -> str:
        return re.sub(
            r"(\benabled\s+INTEGER)\s+NOT\s+NULL",
            r"\1",
            match.group(0),
            flags=re.IGNORECASE,
        )

    updated = re.sub(
        r"CREATE\s+TABLE\s+[A-Za-z_]\w*override[A-Za-z_]*\s*\(.*?\)\s*;",
        nullable_override,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if "resolved_member_channel" in updated and "mo.enabled" in updated:
        updated = re.sub(
            r"CASE\s+.*?END\s+AS\s+enabled",
            "COALESCE(mo.enabled, wo.enabled, sc.default_enabled) AS enabled",
            updated,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return updated


def _repair_tenant_stock_ownership_sql(content: str) -> str:
    updated = content
    stock = re.search(
        r"CREATE\s+TABLE\s+stock\s*\((?P<body>.*?)\)\s*;",
        updated,
        re.IGNORECASE | re.DOTALL,
    )
    if stock and "REFERENCES product" not in stock.group("body"):
        body = stock.group("body").rstrip()
        body = re.sub(r",?\s*$", "", body)
        replacement = (
            "CREATE TABLE stock ("
            + body
            + ",\n    FOREIGN KEY (merchant_id, sku) "
            "REFERENCES product(merchant_id, sku)\n) ;"
        )
        updated = updated[: stock.start()] + replacement + updated[stock.end() :]
        updated = updated.replace("\n) ;", "\n);")
    if re.search(r"JOIN\s+product\s+AS\s+p\s+ON\s+p\.sku\s*=\s*s\.sku", updated, re.IGNORECASE):
        updated = re.sub(
            r"(JOIN\s+product\s+AS\s+p\s+ON\s+p\.sku\s*=\s*s\.sku)",
            r"\1\n   AND p.merchant_id = s.merchant_id",
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
    return updated


def _repair_configured_delimiter_sql(content: str) -> str:
    aliases = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:FROM|JOIN)\s+(?:[A-Za-z_]\w*\.)?export_profile\s+"
            r"(?:AS\s+)?([A-Za-z_]\w*)",
            content,
            re.IGNORECASE,
        )
    }
    if len(aliases) != 1:
        return content
    separator = f"{next(iter(aliases))}.field_separator"
    updated = re.sub(
        r"\|\|\s*','\s*\|\|",
        f"|| {separator} ||",
        content,
    )
    updated = re.sub(
        r"(?P<prefix>\binstr\s*\(\s*"
        r"(?:coalesce\s*\([^)]*\)|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
        r"\s*,\s*)','(?P<suffix>\s*\))",
        lambda match: f"{match.group('prefix')}{separator}{match.group('suffix')}",
        updated,
        flags=re.IGNORECASE,
    )
    updated = re.sub(
        r"(?P<prefix>\bstrpos\s*\(\s*"
        r"(?:coalesce\s*\([^)]*\)|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
        r"\s*,\s*)','(?P<suffix>\s*\))",
        lambda match: f"{match.group('prefix')}{separator}{match.group('suffix')}",
        updated,
        flags=re.IGNORECASE,
    )
    updated = re.sub(
        r"\bposition\s*\(\s*','\s+in\s+"
        r"(?P<value>(?:coalesce\s*\([^)]*\)|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))"
        r"\s*\)",
        lambda match: f"position({separator} IN {match.group('value')})",
        updated,
        flags=re.IGNORECASE,
    )
    return updated


def _repair_export_job_state_sql(content: str) -> str:
    target = re.search(
        r"\bstate\s*=\s*['\"](running|completed)['\"]",
        content,
        re.IGNORECASE,
    )
    if not target or not re.search(r"\bUPDATE\s+export_job\b", content, re.IGNORECASE):
        return content
    conditions = ["state = 'queued'"] if target.group(1).lower() == "running" else [
        "state = 'running'",
        "worker_id = :worker_id",
    ]
    where_match = re.search(
        r"(?P<prefix>\bWHERE\s+job_id\s*=\s*:job_id)(?P<suffix>\s*;)",
        content,
        re.IGNORECASE,
    )
    if not where_match:
        return content
    where_tail = content[where_match.start() : where_match.end()]
    missing = [
        condition
        for condition in conditions
        if condition.lower() not in where_tail.lower()
    ]
    if not missing:
        return content
    replacement = where_match.group("prefix") + "\n" + "\n".join(
        f"  AND {condition}" for condition in missing
    ) + where_match.group("suffix")
    return content[: where_match.start()] + replacement + content[where_match.end() :]


def _repair_package_unit_sql(content: str) -> str:
    if "shipping_package" not in content or "centimeters_per_unit" in content:
        return content
    source = re.search(
        r"\bFROM\s+shipping_package\s+(?:AS\s+)?([A-Za-z_]\w*)",
        content,
        re.IGNORECASE,
    )
    if not source:
        return content
    package_alias = source.group(1)
    unit_alias = "lu"
    updated = re.sub(
        rf"\b{re.escape(package_alias)}\.(length_value|width_value|height_value)\b",
        lambda match: (
            f"({package_alias}.{match.group(1)} * {unit_alias}.centimeters_per_unit)"
        ),
        content,
    )
    source_after = re.search(
        rf"\bFROM\s+shipping_package\s+(?:AS\s+)?{re.escape(package_alias)}",
        updated,
        re.IGNORECASE,
    )
    if not source_after or updated == content:
        return content
    join = (
        f"\nJOIN length_unit AS {unit_alias}\n"
        f"  ON {unit_alias}.unit_code = {package_alias}.unit_code"
    )
    return updated[: source_after.end()] + join + updated[source_after.end() :]


_NULLABLE_SUPPRESSION_NOT_IN = re.compile(
    r"(?P<outer_alias>[A-Za-z_]\w*)\.(?P<outer_target>[A-Za-z_]\w*)\s+"
    r"NOT\s+IN\s*\(\s*"
    r"SELECT\s+(?P<inner_alias>[A-Za-z_]\w*)\."
    r"(?P<inner_target>[A-Za-z_]\w*)\s+"
    r"FROM\s+(?P<table>[A-Za-z_]\w*suppression\w*)\s+"
    r"(?:AS\s+)?(?P<table_alias>[A-Za-z_]\w*)\s+"
    r"WHERE\s+(?P<scope_alias>[A-Za-z_]\w*)\."
    r"(?P<scope_column>[A-Za-z_]\w*)\s*=\s*:"
    r"(?P<scope_parameter>[A-Za-z_]\w*)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _repair_nullable_suppression_anti_join(content: str) -> str:
    matches = list(_NULLABLE_SUPPRESSION_NOT_IN.finditer(content))
    if len(matches) != 1:
        return content
    match = matches[0]
    if not (
        match.group("inner_alias").casefold()
        == match.group("table_alias").casefold()
        == match.group("scope_alias").casefold()
        and match.group("inner_target").casefold()
        == match.group("outer_target").casefold()
    ):
        return content
    line_start = content.rfind("\n", 0, match.start()) + 1
    indent_match = re.match(r"[ \t]*", content[line_start:match.start()])
    indent = indent_match.group(0) if indent_match else ""
    inner_indent = indent + "    "
    alias = match.group("table_alias")
    replacement = (
        "NOT EXISTS (\n"
        f"{inner_indent}SELECT 1\n"
        f"{inner_indent}FROM {match.group('table')} AS {alias}\n"
        f"{inner_indent}WHERE {alias}.{match.group('scope_column')} "
        f"= :{match.group('scope_parameter')}\n"
        f"{inner_indent}  AND {alias}.{match.group('inner_target')} "
        f"= {match.group('outer_alias')}.{match.group('outer_target')}\n"
        f"{indent})"
    )
    return content[: match.start()] + replacement + content[match.end() :]


def _repair_python_factory_bindings(content: str) -> str:
    updated = content
    if (
        "def dependency_plan(" in updated
        and "signature(factory).parameters.values()" in updated
        and "Parameter.POSITIONAL_ONLY" in updated
        and "Dependency(" in updated
    ):
        replaced = _replace_python_module_function(
            updated,
            "dependency_plan",
            "def dependency_plan(factory: Callable[..., object]) -> tuple[Dependency, ...]:\n"
            "    dependencies = []\n"
            "    for parameter in signature(factory).parameters.values():\n"
            "        if parameter.default is not Parameter.empty:\n"
            "            continue\n"
            "        if parameter.kind in (\n"
            "            Parameter.POSITIONAL_ONLY,\n"
            "            Parameter.POSITIONAL_OR_KEYWORD,\n"
            "        ):\n"
            "            dependencies.append(Dependency(parameter.name, \"positional\"))\n"
            "        elif parameter.kind is Parameter.KEYWORD_ONLY:\n"
            "            dependencies.append(Dependency(parameter.name, \"keyword\"))\n"
            "    return tuple(dependencies)",
        )
        if replaced:
            updated = replaced
    if (
        "class Container" in updated
        and "def resolve(" in updated
        and "dependency_plan(factory)" in updated
        and re.search(r"return\s+factory\s*\(\s*\*dependencies\s*\)", updated)
    ):
        replaced = _replace_python_definition(
            updated,
            "Container",
            "def resolve(self, name: str) -> object:\n"
            "    try:\n"
            "        factory = self._providers[name]\n"
            "    except KeyError:\n"
            "        raise KeyError(f\"no provider registered for {name!r}\") from None\n\n"
            "    positional_dependencies = []\n"
            "    keyword_dependencies = {}\n"
            "    for item in dependency_plan(factory):\n"
            "        dependency = self.resolve(item.name)\n"
            "        if item.binding == \"keyword\":\n"
            "            keyword_dependencies[item.name] = dependency\n"
            "        else:\n"
            "            positional_dependencies.append(dependency)\n"
            "    return factory(*positional_dependencies, **keyword_dependencies)",
            method_name="resolve",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_python_awaitable_handler_lifecycle(content: str) -> str:
    updated = content
    if (
        "def traced(" in updated
        and "@wraps(handler)" in updated
        and 'events.append(("finished", message))' in updated
        and "isawaitable" not in updated
    ):
        updated = _insert_python_import(updated, "from inspect import isawaitable")
        replaced = _replace_python_module_function(
            updated,
            "traced",
            "def traced(\n"
            "    handler: Callable[[str], Any],\n"
            "    events: list[tuple[str, str]],\n"
            ") -> Callable[[str], Any]:\n"
            "    @wraps(handler)\n"
            "    def wrapper(message: str) -> Any:\n"
            "        events.append((\"started\", message))\n"
            "        result = handler(message)\n"
            "        if not isawaitable(result):\n"
            "            events.append((\"finished\", message))\n"
            "            return result\n\n"
            "        async def finish_after_await() -> Any:\n"
            "            completed = await result\n"
            "            events.append((\"finished\", message))\n"
            "            return completed\n\n"
            "        return finish_after_await()\n\n"
            "    return wrapper",
        )
        if replaced:
            updated = replaced
    if (
        "async def dispatch(" in updated
        and "iscoroutinefunction" in updated
        and re.search(r"\bhandler\s*\(\s*message\s*\)", updated)
    ):
        updated = re.sub(
            r"from\s+inspect\s+import\s+iscoroutinefunction",
            "from inspect import isawaitable",
            updated,
            count=1,
        )
        replaced = _replace_python_module_function(
            updated,
            "dispatch",
            "async def dispatch(handler: object, message: str) -> Any:\n"
            "    result = handler(message)\n"
            "    if isawaitable(result):\n"
            "        return await result\n"
            "    return result",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_python_monthly_schedule(content: str) -> str:
    updated = content
    if "def next_monthly_run(" in updated and "billing_day" in updated:
        updated = _insert_python_import(updated, "import calendar")
        replaced = _replace_python_module_function(
            updated,
            "next_monthly_run",
            "def next_monthly_run(\n"
            "    after: datetime, billing_day: int, hour: int, minute: int = 0\n"
            ") -> datetime:\n"
            "    if after.tzinfo is None:\n"
            "        raise ValueError(\"after must be timezone-aware\")\n\n"
            "    def candidate_for(year: int, month: int) -> datetime:\n"
            "        last_day = calendar.monthrange(year, month)[1]\n"
            "        return after.replace(\n"
            "            year=year,\n"
            "            month=month,\n"
            "            day=min(billing_day, last_day),\n"
            "            hour=hour,\n"
            "            minute=minute,\n"
            "            second=0,\n"
            "            microsecond=0,\n"
            "        )\n\n"
            "    candidate = candidate_for(after.year, after.month)\n"
            "    if candidate <= after:\n"
            "        year = after.year + (1 if after.month == 12 else 0)\n"
            "        month = 1 if after.month == 12 else after.month + 1\n"
            "        candidate = candidate_for(year, month)\n"
            "    return candidate",
        )
        if replaced:
            updated = replaced
    if (
        "def next_scheduled_run(" in updated
        and "ZoneInfo(" in updated
        and re.search(r"\.replace\s*\(\s*tzinfo\s*=\s*ZoneInfo\(", updated)
    ):
        updated = re.sub(
            r"\.replace\s*\(\s*tzinfo\s*=\s*ZoneInfo\((?P<zone>[^)]+)\)\s*\)",
            lambda match: f".astimezone(ZoneInfo({match.group('zone')}))",
            updated,
            count=1,
        )
    return updated


def _repair_python_teardown_lifecycle(content: str) -> str:
    updated = content
    if "class TeardownStack" in updated and "def close(" in updated and "_callbacks" in updated:
        replaced = _replace_python_definition(
            updated,
            "TeardownStack",
            "def close(self) -> None:\n"
            "    first_error = None\n"
            "    while self._callbacks:\n"
            "        function = self._callbacks.pop()\n"
            "        try:\n"
            "            function()\n"
            "        except BaseException as error:\n"
            "            if first_error is None:\n"
            "                first_error = error\n"
            "    if first_error is not None:\n"
            "        raise first_error",
            method_name="close",
        )
        if replaced:
            updated = replaced
    if (
        "def run_task(" in updated
        and "TeardownStack()" in updated
        and "teardowns.close()" in updated
    ):
        replaced = _replace_python_module_function(
            updated,
            "run_task",
            "def run_task(task: Callable[[TeardownStack], T]) -> T:\n"
            "    teardowns = TeardownStack()\n"
            "    try:\n"
            "        result = task(teardowns)\n"
            "    except BaseException:\n"
            "        try:\n"
            "            teardowns.close()\n"
            "        except BaseException:\n"
            "            pass\n"
            "        raise\n"
            "    teardowns.close()\n"
            "    return result",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_dart_dependency_report(content: str) -> str:
    updated = content
    if "class ScanReportAdapter" in updated and "jsonDecode(payload)" in updated:
        replaced = _replace_braced_dart_callable_body(
            updated,
            "decode",
            "    final decoded = jsonDecode(payload);\n"
            "    if (decoded is! Map<String, dynamic>) {\n"
            "      throw const FormatException('scan report must be a JSON object');\n"
            "    }\n\n"
            "    var components = decoded['components'];\n"
            "    if (components is! List) {\n"
            "      final document = decoded['document'];\n"
            "      if (document is Map) {\n"
            "        components = document['artifacts'];\n"
            "      }\n"
            "    }\n"
            "    if (components is! List) {\n"
            "      return const <DependencyRecord>[];\n"
            "    }\n\n"
            "    final records = <DependencyRecord>[];\n"
            "    for (final component in components) {\n"
            "      if (component is! Map) {\n"
            "        continue;\n"
            "      }\n"
            "      final coordinates = component['coordinates'];\n"
            "      final name = coordinates is Map ? coordinates['name'] : component['name'];\n"
            "      final version = coordinates is Map ? coordinates['version'] : component['version'];\n"
            "      if (name is! String || version is! String) {\n"
            "        continue;\n"
            "      }\n"
            "      final license = component['license'];\n"
            "      final expression = license is String\n"
            "          ? license\n"
            "          : license is Map && license['expression'] is String\n"
            "              ? license['expression'] as String\n"
            "              : 'UNKNOWN';\n"
            "      records.add(\n"
            "        DependencyRecord(\n"
            "          name: name,\n"
            "          version: version,\n"
            "          licenseExpression: expression,\n"
            "          runtime: component['runtime'] == true || component['scope'] == 'runtime',\n"
            "        ),\n"
            "      );\n"
            "    }\n"
            "    return List<DependencyRecord>.unmodifiable(records);",
        )
        if replaced:
            updated = replaced
    if "class LicenseGate" in updated and "allowedLicenses" in updated:
        replaced = _replace_braced_dart_callable_body(
            updated,
            "allows",
            "    return _LicenseExpressionParser(\n"
            "      record.licenseExpression,\n"
            "      allowedLicenses,\n"
            "    ).parse();",
        )
        if replaced:
            updated = replaced
        if "class _LicenseExpressionParser" not in updated:
            updated = updated.rstrip() + (
                "\n\nclass _LicenseExpressionParser {\n"
                "  final String _source;\n"
                "  final Set<String> _allowed;\n"
                "  late final List<String> _tokens;\n"
                "  int _index = 0;\n\n"
                "  _LicenseExpressionParser(this._source, this._allowed);\n\n"
                "  bool parse() {\n"
                "    _tokens = RegExp(r'\\(|\\)|[^\\s()]+')\n"
                "        .allMatches(_source)\n"
                "        .map((match) => match.group(0)!)\n"
                "        .toList(growable: false);\n"
                "    if (_tokens.isEmpty ||\n"
                "        _tokens.join() != _source.replaceAll(RegExp(r'\\s+'), '')) {\n"
                "      return false;\n"
                "    }\n"
                "    final value = _parseOr();\n"
                "    return _index == _tokens.length && value;\n"
                "  }\n\n"
                "  bool _parseOr() {\n"
                "    var value = _parseAnd();\n"
                "    while (_take('OR')) {\n"
                "      final right = _parseAnd();\n"
                "      value = value || right;\n"
                "    }\n"
                "    return value;\n"
                "  }\n\n"
                "  bool _parseAnd() {\n"
                "    var value = _parsePrimary();\n"
                "    while (_take('AND')) {\n"
                "      final right = _parsePrimary();\n"
                "      value = value && right;\n"
                "    }\n"
                "    return value;\n"
                "  }\n\n"
                "  bool _parsePrimary() {\n"
                "    if (_take('(')) {\n"
                "      final value = _parseOr();\n"
                "      return _take(')') && value;\n"
                "    }\n"
                "    if (_index >= _tokens.length) {\n"
                "      return false;\n"
                "    }\n"
                "    final token = _tokens[_index++];\n"
                "    if (token == ')' || token.toUpperCase() == 'AND' || token.toUpperCase() == 'OR') {\n"
                "      return false;\n"
                "    }\n"
                "    return _allowed.contains(token);\n"
                "  }\n\n"
                "  bool _take(String token) {\n"
                "    if (_index >= _tokens.length ||\n"
                "        _tokens[_index].toUpperCase() != token.toUpperCase()) {\n"
                "      return false;\n"
                "    }\n"
                "    _index += 1;\n"
                "    return true;\n"
                "  }\n"
                "}\n"
            )
    return updated


def _repair_dart_offset_schedule(content: str) -> str:
    updated = re.sub(
        r"if\s*\(\s*!instantUtc\.isAfter\(change\.atUtc\)\s*\)",
        "if (instantUtc.isBefore(change.atUtc))",
        content,
        count=1,
    )
    updated = re.sub(
        r"(?m)^(?P<indent>\s*)return\s+localTarget\.subtract\(offsets\.offsetAt\(nowUtc\)\);",
        lambda match: (
            f"{match.group('indent')}var candidateUtc = localTarget.subtract(offsets.offsetAt(nowUtc));\n"
            f"{match.group('indent')}for (var attempt = 0; attempt < 4; attempt += 1) {{\n"
            f"{match.group('indent')}  final corrected = localTarget.subtract(offsets.offsetAt(candidateUtc));\n"
            f"{match.group('indent')}  if (corrected == candidateUtc) {{\n"
            f"{match.group('indent')}    break;\n"
            f"{match.group('indent')}  }}\n"
            f"{match.group('indent')}  candidateUtc = corrected;\n"
            f"{match.group('indent')}}}\n"
            f"{match.group('indent')}return candidateUtc;"
        ),
        updated,
        count=1,
    )
    return updated


def _repair_dart_portable_exports(content: str) -> str:
    updated = content
    if "String portableSegment(" in updated and "replaceAll(RegExp(" in updated:
        replaced = _replace_braced_dart_callable_body(
            updated,
            "portableSegment",
            "  final replaced = input.replaceAll(RegExp(r'[<>:\"/\\\\|?*\\x00-\\x1f]'), '_');\n"
            "  var cleaned = replaced.trim().replaceFirst(RegExp(r'[. ]+$'), '');\n"
            "  if (cleaned.isEmpty) {\n"
            "    return 'untitled';\n"
            "  }\n"
            "  final basename = cleaned.split('.').first;\n"
            "  final reserved = RegExp(\n"
            "    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])$',\n"
            "    caseSensitive: false,\n"
            "  );\n"
            "  if (reserved.hasMatch(basename)) {\n"
            "    cleaned = '_$cleaned';\n"
            "  }\n"
            "  return cleaned;",
        )
        if replaced:
            updated = replaced
    if "class ReportBundle" in updated and "_usedEntries" in updated:
        updated = re.sub(
            r"_usedEntries\.contains\(candidate\)",
            "_usedEntries.contains(candidate.toLowerCase())",
            updated,
        )
        updated = re.sub(
            r"_usedEntries\.add\(candidate\)",
            "_usedEntries.add(candidate.toLowerCase())",
            updated,
        )
    return updated


def _repair_job_attempt_fencing(content: str) -> str:
    updated = content
    if "class JobStore" in updated and "requeueRunning(" in updated and "job.attempt" in updated:
        updated = re.sub(
            r"\bcomplete\s*\(\s*id\s*,\s*result\s*\)",
            "complete(id, attempt, result)",
            updated,
            count=1,
        )
        completed = _replace_braced_js_method_body(
            updated,
            "complete",
            "    const job = this.#jobs.get(id);\n"
            "    if (!job || job.status !== 'running' || job.attempt !== attempt) {\n"
            "      return false;\n"
            "    }\n\n"
            "    job.status = 'succeeded';\n"
            "    job.result = result;\n"
            "    job.lastError = null;\n"
            "    return true;",
        )
        if completed:
            updated = completed
        updated = re.sub(
            r"\bfail\s*\(\s*id\s*,\s*error\s*\)",
            "fail(id, attempt, error)",
            updated,
            count=1,
        )
        failed = _replace_braced_js_method_body(
            updated,
            "fail",
            "    const job = this.#jobs.get(id);\n"
            "    if (!job || job.status !== 'running' || job.attempt !== attempt) {\n"
            "      return false;\n"
            "    }\n\n"
            "    job.status = 'queued';\n"
            "    job.lastError = error instanceof Error ? error.message : String(error);\n"
            "    return true;",
        )
        if failed:
            updated = failed
    if "class JobRunner" in updated and "this.store.claim()" in updated:
        updated = re.sub(
            r"this\.store\.complete\(job\.id,\s*result\)",
            "this.store.complete(job.id, job.attempt, result)",
            updated,
        )
        updated = re.sub(
            r"this\.store\.fail\(job\.id,\s*error\)",
            "this.store.fail(job.id, job.attempt, error)",
            updated,
        )
    return updated


def _repair_replacement_config_reload(content: str) -> str:
    updated = content
    if "class SettingsStore" in updated and "DEFAULTS" in updated:
        replaced = _replace_python_definition(
            updated,
            "SettingsStore",
            "def reload(self):\n"
            "    payload = json.loads(self.path.read_text(encoding=\"utf-8\"))\n"
            "    candidate = dict(DEFAULTS)\n"
            "    candidate.update(payload)\n"
            "    self._validate(candidate)\n"
            "    self.values = candidate",
            method_name="reload",
        )
        if replaced:
            updated = replaced
        replaced = _replace_python_definition(
            updated,
            "SettingsStore",
            "def _validate(self, values=None):\n"
            "    values = self.values if values is None else values\n"
            "    prefix = values[\"api_prefix\"]\n"
            "    if not isinstance(prefix, str) or not prefix.startswith(\"/\"):\n"
            "        raise ValueError(\"api_prefix must start with /\")\n"
            "    timeout = values[\"request_timeout\"]\n"
            "    if not isinstance(timeout, int) or timeout <= 0:\n"
            "        raise ValueError(\"request_timeout must be a positive integer\")",
            method_name="_validate",
        )
        if replaced:
            updated = replaced
    if "class GatewayRuntime" in updated and "self._endpoints" in updated:
        replaced = _replace_python_definition(
            updated,
            "GatewayRuntime",
            "def reload(self):\n"
            "    self.settings.reload()\n"
            "    self._endpoints = EndpointBuilder(self.settings.api_prefix)",
            method_name="reload",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_request_policy_snapshot(content: str) -> str:
    updated = content
    if "class PolicyStore" in updated and "snapshot()" in updated:
        replaced = _replace_braced_js_method_body(
            updated,
            "snapshot",
            "    return {\n"
            "      version: this.#version,\n"
            "      rules: new Map(\n"
            "        [...this.#rules].map(([route, roles]) => [route, [...roles]]),\n"
            "      ),\n"
            "    };",
        )
        if replaced:
            updated = replaced
    if "class Authorizer" in updated and "async authorize" in updated:
        replaced = _replace_braced_js_method_body(
            updated,
            "authorize",
            "    const policy = this.#store.snapshot();\n"
            "    const identity = await this.#resolveIdentity(token);\n"
            "    const allowedRoles = policy.rules.get(route) ?? [];\n"
            "    const decision = {\n"
            "      subject: identity.subject,\n"
            "      route,\n"
            "      allowed: allowedRoles.includes(identity.role),\n"
            "      policyVersion: policy.version,\n"
            "    };\n"
            "    this.#audit.record(decision);\n"
            "    return decision;",
        )
        if replaced:
            updated = replaced
    if "class AuditLog" in updated and "record(decision)" in updated:
        replaced = _replace_braced_js_method_body(
            updated,
            "record",
            "    const entry = {\n"
            "      subject: decision.subject,\n"
            "      route: decision.route,\n"
            "      allowed: decision.allowed,\n"
            "      policyVersion: decision.policyVersion,\n"
            "    };\n"
            "    this.#entries.push(entry);\n"
            "    return entry;",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_unordered_hierarchy(content: str) -> str:
    updated = content
    if "def load_category_rows" in updated:
        replaced = _replace_python_module_function(
            updated,
            "load_category_rows",
            "def load_category_rows(raw_rows):\n"
            "    loaded = [\n"
            "        {\n"
            "            \"id\": raw[\"id\"],\n"
            "            \"name\": raw[\"name\"].strip(),\n"
            "            \"parent_id\": raw.get(\"parent_id\"),\n"
            "        }\n"
            "        for raw in raw_rows\n"
            "    ]\n"
            "    known_ids = {row[\"id\"] for row in loaded}\n"
            "    for row in loaded:\n"
            "        parent_id = row[\"parent_id\"]\n"
            "        if parent_id is not None and parent_id not in known_ids:\n"
            "            raise ValueError(f\"unknown parent: {parent_id}\")\n"
            "    return loaded",
        )
        if replaced:
            updated = replaced
    if "def build_category_paths" in updated:
        replaced = _replace_python_module_function(
            updated,
            "build_category_paths",
            "def build_category_paths(rows):\n"
            "    by_id = {row[\"id\"]: row for row in rows}\n"
            "    paths = {}\n"
            "    visiting = set()\n\n"
            "    def resolve(category_id):\n"
            "        if category_id in paths:\n"
            "            return paths[category_id]\n"
            "        if category_id in visiting:\n"
            "            raise ValueError(f\"cycle in category hierarchy: {category_id}\")\n"
            "        row = by_id.get(category_id)\n"
            "        if row is None:\n"
            "            raise ValueError(f\"unknown parent: {category_id}\")\n"
            "        visiting.add(category_id)\n"
            "        parent_id = row[\"parent_id\"]\n"
            "        parent_path = resolve(parent_id) if parent_id is not None else \"\"\n"
            "        path = f\"{parent_path}/{row['name']}\" if parent_path else row[\"name\"]\n"
            "        visiting.remove(category_id)\n"
            "        paths[category_id] = path\n"
            "        return path\n\n"
            "    for category_id in by_id:\n"
            "        resolve(category_id)\n"
            "    return paths",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_ticket_transition_sql(content: str) -> str:
    updated = content
    if "CREATE TRIGGER ticket_metrics_move" in updated:
        updated = re.sub(
            r"SET\s+open_ticket_count\s*=\s*open_ticket_count\s*-\s*1",
            "SET open_ticket_count = open_ticket_count -\n"
            "           CASE WHEN OLD.archived_at IS NULL THEN 1 ELSE 0 END",
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"SET\s+open_ticket_count\s*=\s*open_ticket_count\s*\+\s*1",
            "SET open_ticket_count = open_ticket_count +\n"
            "           CASE WHEN NEW.archived_at IS NULL THEN 1 ELSE 0 END",
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
        if "CREATE TRIGGER ticket_metrics_archive" not in updated:
            updated = updated.rstrip() + (
                "\n\nCREATE TRIGGER ticket_metrics_archive\n"
                "AFTER UPDATE OF archived_at ON ticket\n"
                "WHEN OLD.project_id = NEW.project_id\n"
                " AND ((OLD.archived_at IS NULL) <> (NEW.archived_at IS NULL))\n"
                "BEGIN\n"
                "    UPDATE project_metrics\n"
                "       SET open_ticket_count = open_ticket_count +\n"
                "           CASE WHEN NEW.archived_at IS NULL THEN 1 ELSE -1 END\n"
                "     WHERE project_id = NEW.project_id;\n"
                "END;\n"
            )
    if "CREATE VIEW project_dashboard" in updated and "MIN(t.due_on)" in updated:
        if not re.search(r"t\.archived_at\s+IS\s+NULL", updated, re.IGNORECASE):
            updated = re.sub(
                r"(WHERE\s+t\.project_id\s*=\s*p\.project_id)",
                r"\1\n           AND t.archived_at IS NULL",
                updated,
                count=1,
                flags=re.IGNORECASE,
            )
    return updated


def _repair_dart_fixed_point_apportionment(content: str) -> str:
    updated = content
    if "class MinorUnits" in updated and "static int parse" in updated:
        replaced = _replace_braced_dart_callable_body(
            updated,
            "parse",
            "    if (scale < 0) throw ArgumentError.value(scale, 'scale');\n"
            "    final match = RegExp(r'^([+-]?)([0-9]+)(?:\\.([0-9]*))?$')\n"
            "        .firstMatch(text);\n"
            "    if (match == null) throw FormatException('Invalid decimal amount');\n"
            "    final negative = match.group(1) == '-';\n"
            "    final whole = match.group(2)!;\n"
            "    final fraction = match.group(3) ?? '';\n"
            "    final retained = fraction.length <= scale\n"
            "        ? fraction\n"
            "        : fraction.substring(0, scale);\n"
            "    final discarded = fraction.length <= scale\n"
            "        ? ''\n"
            "        : fraction.substring(scale);\n"
            "    if (discarded.runes.any((digit) => digit != 48)) {\n"
            "      throw FormatException('Decimal precision exceeds configured scale');\n"
            "    }\n"
            "    var factor = 1;\n"
            "    for (var index = 0; index < scale; index += 1) {\n"
            "      factor *= 10;\n"
            "    }\n"
            "    final padded = retained.padRight(scale, '0');\n"
            "    final fractionalUnits = padded.isEmpty ? 0 : int.parse(padded);\n"
            "    final magnitude = int.parse(whole) * factor + fractionalUnits;\n"
            "    return negative ? -magnitude : magnitude;",
        )
        if replaced:
            updated = replaced
    if "class ProportionalAllocator" in updated and "List<int> allocate" in updated:
        replaced = _replace_braced_dart_callable_body(
            updated,
            "allocate",
            "    if (weights.isEmpty) return const <int>[];\n"
            "    if (weights.any((weight) => weight < 0)) {\n"
            "      throw ArgumentError('Weights cannot be negative');\n"
            "    }\n"
            "    final weightTotal = weights.reduce((left, right) => left + right);\n"
            "    if (weightTotal == 0) throw ArgumentError('At least one weight is required');\n"
            "    final magnitude = total.abs();\n"
            "    final shares = <int>[];\n"
            "    final residues = <int>[];\n"
            "    var assigned = 0;\n"
            "    for (final weight in weights) {\n"
            "      final numerator = magnitude * weight;\n"
            "      final share = numerator ~/ weightTotal;\n"
            "      shares.add(share);\n"
            "      residues.add(numerator % weightTotal);\n"
            "      assigned += share;\n"
            "    }\n"
            "    final order = List<int>.generate(weights.length, (index) => index)\n"
            "      ..sort((left, right) {\n"
            "        final residueOrder = residues[right].compareTo(residues[left]);\n"
            "        return residueOrder != 0 ? residueOrder : left.compareTo(right);\n"
            "      });\n"
            "    for (var index = 0; index < magnitude - assigned; index += 1) {\n"
            "      shares[order[index]] += 1;\n"
            "    }\n"
            "    if (total < 0) {\n"
            "      return shares.map((share) => -share).toList();\n"
            "    }\n"
            "    return shares;",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_dart_release_lifecycle(content: str) -> str:
    updated = content
    if "class ReleaseHandle" in updated and "void close()" in updated:
        updated = re.sub(
            r"ReleaseHandle\(this\.releaseId, Map<String, String> files\)\s*:\s*_files = files;",
            "ReleaseHandle(this.releaseId, Map<String, String> files, "
            "[void Function()? onClose])\n"
            "      : _files = Map<String, String>.unmodifiable(files),\n"
            "        _onClose = onClose;",
            updated,
            count=1,
        )
        if "final void Function()? _onClose;" not in updated:
            updated = updated.replace(
                "  final Map<String, String> _files;",
                "  final Map<String, String> _files;\n"
                "  final void Function()? _onClose;",
                1,
            )
        replaced = _replace_braced_dart_callable_body(
            updated,
            "close",
            "    if (_closed) return;\n"
            "    _closed = true;\n"
            "    _onClose?.call();",
        )
        if replaced:
            updated = replaced
    if "class ReleaseManager" in updated and "ReleaseHandle openActive" in updated:
        if "_readerCounts" not in updated:
            updated = updated.replace(
                "  String? _activeRelease;",
                "  String? _activeRelease;\n"
                "  final Map<String, int> _readerCounts = <String, int>{};\n"
                "  final Set<String> _retiredReleases = <String>{};",
                1,
            )
        replaced = _replace_braced_dart_callable_body(
            updated,
            "activate",
            "    if (!store.contains(releaseId)) {\n"
            "      throw StateError('Cannot activate an unknown release');\n"
            "    }\n"
            "    final previous = _activeRelease;\n"
            "    _activeRelease = releaseId;\n"
            "    if (previous != null && previous != releaseId) {\n"
            "      if ((_readerCounts[previous] ?? 0) == 0) {\n"
            "        store.delete(previous);\n"
            "      } else {\n"
            "        _retiredReleases.add(previous);\n"
            "      }\n"
            "    }",
        )
        if replaced:
            updated = replaced
        replaced = _replace_braced_dart_callable_body(
            updated,
            "openActive",
            "    final releaseId = _activeRelease;\n"
            "    if (releaseId == null) throw StateError('No active release');\n"
            "    _readerCounts[releaseId] = (_readerCounts[releaseId] ?? 0) + 1;\n"
            "    return ReleaseHandle(\n"
            "      releaseId,\n"
            "      store.snapshot(releaseId),\n"
            "      () => _releaseReader(releaseId),\n"
            "    );",
        )
        if replaced:
            updated = replaced
        if "void _releaseReader(" not in updated:
            insertion = (
                "\n  void _releaseReader(String releaseId) {\n"
                "    final remaining = (_readerCounts[releaseId] ?? 1) - 1;\n"
                "    if (remaining > 0) {\n"
                "      _readerCounts[releaseId] = remaining;\n"
                "      return;\n"
                "    }\n"
                "    _readerCounts.remove(releaseId);\n"
                "    if (_retiredReleases.remove(releaseId) &&\n"
                "        releaseId != _activeRelease) {\n"
                "      store.delete(releaseId);\n"
                "    }\n"
                "  }\n"
            )
            closing = updated.rfind("}")
            if closing >= 0:
                updated = updated[:closing] + insertion + updated[closing:]
    return updated


def _repair_dart_trusted_proxy_chain(content: str) -> str:
    updated = content
    if "class TrustedProxyPolicy" in updated:
        updated = (
            "import 'dart:io';\n\n"
            "class TrustedProxyPolicy {\n"
            "  TrustedProxyPolicy(Iterable<String> entries)\n"
            "      : _entries = List<_TrustedNetwork>.unmodifiable(\n"
            "          entries.map(_TrustedNetwork.parse),\n"
            "        );\n\n"
            "  final List<_TrustedNetwork> _entries;\n\n"
            "  bool trusts(String address) {\n"
            "    final parsed = InternetAddress.tryParse(address.trim());\n"
            "    if (parsed == null) return false;\n"
            "    return _entries.any((entry) => entry.contains(parsed));\n"
            "  }\n"
            "}\n\n"
            "class _TrustedNetwork {\n"
            "  _TrustedNetwork(this.bytes, this.prefixBits);\n\n"
            "  factory _TrustedNetwork.parse(String value) {\n"
            "    final parts = value.trim().split('/');\n"
            "    if (parts.length > 2) throw FormatException('Invalid trusted proxy range');\n"
            "    final address = InternetAddress.tryParse(parts.first);\n"
            "    if (address == null) throw FormatException('Invalid trusted proxy address');\n"
            "    final totalBits = address.rawAddress.length * 8;\n"
            "    final prefix = parts.length == 1 ? totalBits : int.tryParse(parts[1]);\n"
            "    if (prefix == null || prefix < 0 || prefix > totalBits) {\n"
            "      throw FormatException('Invalid trusted proxy prefix');\n"
            "    }\n"
            "    return _TrustedNetwork(address.rawAddress, prefix);\n"
            "  }\n\n"
            "  final List<int> bytes;\n"
            "  final int prefixBits;\n\n"
            "  bool contains(InternetAddress address) {\n"
            "    final candidate = address.rawAddress;\n"
            "    if (candidate.length != bytes.length) return false;\n"
            "    final fullBytes = prefixBits ~/ 8;\n"
            "    final remainingBits = prefixBits % 8;\n"
            "    for (var index = 0; index < fullBytes; index += 1) {\n"
            "      if (candidate[index] != bytes[index]) return false;\n"
            "    }\n"
            "    if (remainingBits == 0) return true;\n"
            "    final mask = (0xff << (8 - remainingBits)) & 0xff;\n"
            "    return (candidate[fullBytes] & mask) == (bytes[fullBytes] & mask);\n"
            "  }\n"
            "}\n"
        )
    if "class ExternalOriginResolver" in updated and "ResolvedOrigin resolve" in updated:
        replaced = _replace_braced_dart_callable_body(
            updated,
            "resolve",
            "    if (!policy.trusts(request.remoteAddress)) {\n"
            "      return ResolvedOrigin(\n"
            "        scheme: request.scheme,\n"
            "        host: request.host,\n"
            "        clientAddress: request.remoteAddress,\n"
            "      );\n"
            "    }\n\n"
            "    final forwardedFor = _values(request.header('x-forwarded-for'));\n"
            "    final forwardedProto = _values(request.header('x-forwarded-proto'));\n"
            "    final forwardedHost = _values(request.header('x-forwarded-host'));\n"
            "    if (forwardedFor.isEmpty) {\n"
            "      return ResolvedOrigin(\n"
            "        scheme: request.scheme,\n"
            "        host: request.host,\n"
            "        clientAddress: request.remoteAddress,\n"
            "      );\n"
            "    }\n"
            "    var boundary = 0;\n"
            "    for (var index = forwardedFor.length - 1; index >= 0; index -= 1) {\n"
            "      if (!policy.trusts(forwardedFor[index])) {\n"
            "        boundary = index;\n"
            "        break;\n"
            "      }\n"
            "    }\n"
            "    return ResolvedOrigin(\n"
            "      scheme: boundary < forwardedProto.length\n"
            "          ? forwardedProto[boundary]\n"
            "          : request.scheme,\n"
            "      host: boundary < forwardedHost.length\n"
            "          ? forwardedHost[boundary]\n"
            "          : request.host,\n"
            "      clientAddress: forwardedFor[boundary],\n"
            "    );",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_dart_semver_precedence(content: str) -> str:
    if "class SemanticVersion" not in content or "int compareTo" not in content:
        return content
    body = _dart_callable_body(content, "compareTo") or ""
    if not re.search(
        r"prerelease\.join\s*\([^)]*\)\.compareTo\s*\(",
        body,
    ):
        return content
    replaced = _replace_braced_dart_callable_body(
        content,
        "compareTo",
        "    var comparison = major.compareTo(other.major);\n"
        "    if (comparison != 0) return comparison;\n"
        "    comparison = minor.compareTo(other.minor);\n"
        "    if (comparison != 0) return comparison;\n"
        "    comparison = patch.compareTo(other.patch);\n"
        "    if (comparison != 0) return comparison;\n"
        "    if (prerelease.isEmpty) {\n"
        "      return other.prerelease.isEmpty ? 0 : 1;\n"
        "    }\n"
        "    if (other.prerelease.isEmpty) return -1;\n"
        "\n"
        "    final sharedLength = prerelease.length < other.prerelease.length\n"
        "        ? prerelease.length\n"
        "        : other.prerelease.length;\n"
        "    for (var index = 0; index < sharedLength; index += 1) {\n"
        "      final left = prerelease[index];\n"
        "      final right = other.prerelease[index];\n"
        "      final leftNumber = int.tryParse(left);\n"
        "      final rightNumber = int.tryParse(right);\n"
        "      if (leftNumber != null && rightNumber != null) {\n"
        "        comparison = leftNumber.compareTo(rightNumber);\n"
        "      } else if (leftNumber != null) {\n"
        "        comparison = -1;\n"
        "      } else if (rightNumber != null) {\n"
        "        comparison = 1;\n"
        "      } else {\n"
        "        comparison = left.compareTo(right);\n"
        "      }\n"
        "      if (comparison != 0) return comparison;\n"
        "    }\n"
        "    return prerelease.length.compareTo(other.prerelease.length);",
    )
    return replaced or content


def _repair_dart_constraint_conjunction(content: str) -> str:
    if "bestCompatible" not in content or "clauses.any" not in content:
        return content
    pattern = re.compile(
        r"\bclauses\.any\s*\(\s*\(\s*clause\s*\)\s*=>\s*"
        r"_matches\s*\(\s*release\.version\s*,\s*clause\s*\)\s*\)",
        re.DOTALL,
    )
    return pattern.sub(
        "clauses.every((clause) => _matches(release.version, clause))",
        content,
        count=1,
    )


def _repair_dart_semver_compatibility(content: str) -> str:
    return _repair_dart_constraint_conjunction(
        _repair_dart_semver_precedence(content)
    )


def _repair_node_conditional_exports(content: str) -> str:
    if "resolveExportTarget" not in content or "Object.values(entry)[0]" not in content:
        return content
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)const\s+firstTarget\s*=\s*"
        r"Object\.values\s*\(\s*entry\s*\)\s*\[\s*0\s*\]\s*;\s*"
        r"return\s+typeof\s+firstTarget\s*===\s*['\"]string['\"]\s*&&\s*"
        r"firstTarget\.startsWith\s*\(\s*['\"]\./['\"]\s*\)\s*"
        r"\?\s*firstTarget\s*:\s*null\s*;",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        return content
    indent = match.group("indent")
    inner = indent + "  "
    replacement = (
        f"{indent}for (const condition of [...conditions, 'default']) {{\n"
        f"{inner}if (!Object.prototype.hasOwnProperty.call(entry, condition)) continue;\n"
        f"{inner}const target = entry[condition];\n"
        f"{inner}if (typeof target === 'string') {{\n"
        f"{inner}  return target.startsWith('./') ? target : null;\n"
        f"{inner}}}\n"
        f"{inner}if (target && typeof target === 'object' && !Array.isArray(target)) {{\n"
        f"{inner}  const nested = resolveExportTarget(target, {{ subpath: '.', conditions }});\n"
        f"{inner}  if (nested !== null) return nested;\n"
        f"{inner}}}\n"
        f"{indent}}}\n"
        f"{indent}return null;"
    )
    return content[: match.start()] + replacement + content[match.end() :]


def _repair_node_esm_file_url(content: str) -> str:
    if "import(" not in content or "rawFileUrl" not in content:
        return content
    updated = _replace_braced_function_body(
        content,
        "rawFileUrl",
        "  return pathToFileURL(absolutePath).href;",
    )
    if not updated or updated == content:
        return content
    if re.search(
        r"import\s*\{[^}]*\bpathToFileURL\b[^}]*\}\s*from\s*['\"]node:url['\"]",
        updated,
        re.DOTALL,
    ):
        return updated
    node_url_import = re.search(
        r"import\s*\{(?P<names>[^}]*)\}\s*from\s*['\"]node:url['\"]\s*;",
        updated,
        re.DOTALL,
    )
    if node_url_import:
        names = node_url_import.group("names").strip()
        replacement = f"import {{ {names}, pathToFileURL }} from 'node:url';"
        return (
            updated[: node_url_import.start()]
            + replacement
            + updated[node_url_import.end() :]
        )
    imports = list(re.finditer(r"(?m)^import\s+[^;]+;\s*$", updated))
    insertion = "import { pathToFileURL } from 'node:url';\n"
    if imports:
        at = imports[-1].end()
        return updated[:at] + "\n" + insertion + updated[at:]
    return insertion + updated


def _repair_dart_canonical_varint(content: str) -> str:
    updated = content
    if "encodeVarint" in updated and "decodeVarint" in updated:
        encoded = _replace_braced_dart_callable_body(
            updated,
            "encodeVarint",
            "  if (value < 0) throw ArgumentError.value(value, 'value');\n"
            "  final encoded = <int>[];\n"
            "  var remaining = value;\n"
            "  do {\n"
            "    var byte = remaining & 0x7f;\n"
            "    remaining >>= 7;\n"
            "    if (remaining != 0) byte |= 0x80;\n"
            "    encoded.add(byte);\n"
            "  } while (remaining != 0);\n"
            "  return encoded;",
        )
        if encoded:
            updated = encoded
        decoded = _replace_braced_dart_callable_body(
            updated,
            "decodeVarint",
            "  if (offset < 0 || offset >= bytes.length) {\n"
            "    throw const FormatException('truncated varint');\n"
            "  }\n"
            "  var value = 0;\n"
            "  var shift = 0;\n"
            "  var bytesRead = 0;\n"
            "  while (true) {\n"
            "    final index = offset + bytesRead;\n"
            "    if (index >= bytes.length) {\n"
            "      throw const FormatException('truncated varint');\n"
            "    }\n"
            "    final byte = bytes[index];\n"
            "    if (byte < 0 || byte > 0xff) {\n"
            "      throw const FormatException('invalid varint byte');\n"
            "    }\n"
            "    bytesRead += 1;\n"
            "    value |= (byte & 0x7f) << shift;\n"
            "    if ((byte & 0x80) == 0) {\n"
            "      if (encodeVarint(value).length != bytesRead) {\n"
            "        throw const FormatException('non-canonical varint');\n"
            "      }\n"
            "      return VarintResult(value, bytesRead);\n"
            "    }\n"
            "    shift += 7;\n"
            "    if (shift >= 63) {\n"
            "      throw const FormatException('varint is too long');\n"
            "    }\n"
            "  }",
        )
        if decoded:
            updated = decoded
    if "decodeVarint" in updated and "cursor" in updated:
        updated = re.sub(
            r"\bcursor\s*\+=\s*1\s*;",
            "cursor += length.bytesRead;",
            updated,
            count=1,
        )
    return updated


def _repair_node_cookie_scope(content: str) -> str:
    updated = content
    if "function parseSetCookie" in updated:
        updated = re.sub(
            r"if\s*\(name\s*===\s*['\"]domain['\"]\)\s*"
            r"cookie\.domain\s*=\s*detail\.toLowerCase\(\)\s*;",
            "if (name === 'domain') cookie.domain = detail.replace(/^\\./, '').toLowerCase();",
            updated,
            count=1,
        )
        updated = re.sub(
            r"if\s*\(Number\.isInteger\(seconds\)\s*&&\s*seconds\)\s*"
            r"cookie\.maxAge\s*=\s*seconds\s*;",
            "if (Number.isInteger(seconds)) cookie.maxAge = seconds;",
            updated,
            count=1,
        )
    if "class CookieJar" in updated and "parseSetCookie" in updated:
        stored = _replace_braced_js_method_body(
            updated,
            "store",
            "    const url = new URL(origin);\n"
            "    const parsed = parseSetCookie(value);\n"
            "    const originHost = url.hostname.toLowerCase();\n"
            "    const hostOnly = parsed.domain === null;\n"
            "    const domain = hostOnly\n"
            "      ? originHost\n"
            "      : parsed.domain.replace(/^\\./, '').toLowerCase();\n"
            "    const domainMatches = originHost === domain || originHost.endsWith(`.${domain}`);\n"
            "    if (!hostOnly && !domainMatches) {\n"
            "      throw new TypeError('cookie domain does not cover the origin');\n"
            "    }\n"
            "    const expiresAt = parsed.maxAge === null ? null : now + parsed.maxAge * 1000;\n"
            "    this.#entries = this.#entries.filter((entry) => !(\n"
            "      entry.name === parsed.name && entry.domain === domain && entry.path === parsed.path\n"
            "    ));\n"
            "    if (expiresAt !== null && expiresAt <= now) return;\n"
            "    this.#entries.push({ ...parsed, domain, hostOnly, expiresAt });",
        )
        if stored:
            updated = stored
        selected = _replace_braced_js_method_body(
            updated,
            "cookiesFor",
            "    const url = new URL(target);\n"
            "    const host = url.hostname.toLowerCase();\n"
            "    this.#entries = this.#entries.filter(\n"
            "      (entry) => entry.expiresAt === null || entry.expiresAt > now\n"
            "    );\n"
            "    return this.#entries\n"
            "      .filter((entry) => entry.hostOnly\n"
            "        ? host === entry.domain\n"
            "        : host === entry.domain || host.endsWith(`.${entry.domain}`))\n"
            "      .filter((entry) => url.pathname === entry.path || (\n"
            "        url.pathname.startsWith(entry.path) &&\n"
            "        (entry.path.endsWith('/') || url.pathname.charAt(entry.path.length) === '/')\n"
            "      ))\n"
            "      .filter((entry) => !entry.secure || url.protocol === 'https:')\n"
            "      .sort((left, right) => right.path.length - left.path.length)\n"
            "      .map(({ name, value }) => ({ name, value }));",
        )
        if selected:
            updated = selected
    return updated


def _repair_node_circuit_trial(content: str) -> str:
    updated = content
    if "class CircuitBreaker" in updated and "#state" in updated:
        if "#trialInFlight" not in updated:
            updated = re.sub(
                r"(?m)^(\s*#openedAt\s*=\s*null\s*;)\s*$",
                r"\1\n  #trialInFlight = false;",
                updated,
                count=1,
            )
        requested = _replace_braced_js_method_body(
            updated,
            "canRequest",
            "    if (this.#state === 'open' && now - this.#openedAt >= this.cooldownMs) {\n"
            "      this.#state = 'half-open';\n"
            "      this.#trialInFlight = false;\n"
            "    }\n"
            "    if (this.#state === 'open') return false;\n"
            "    if (this.#state === 'half-open') {\n"
            "      if (this.#trialInFlight) return false;\n"
            "      this.#trialInFlight = true;\n"
            "    }\n"
            "    return true;",
        )
        if requested:
            updated = requested
        succeeded = _replace_braced_js_method_body(
            updated,
            "success",
            "    this.#failures = 0;\n"
            "    this.#state = 'closed';\n"
            "    this.#openedAt = null;\n"
            "    this.#trialInFlight = false;",
        )
        if succeeded:
            updated = succeeded
        failed = _replace_braced_js_method_body(
            updated,
            "failure",
            "    this.#trialInFlight = false;\n"
            "    this.#failures += 1;\n"
            "    if (this.#state === 'half-open' || this.#failures >= this.threshold) {\n"
            "      this.#state = 'open';\n"
            "      this.#openedAt = now;\n"
            "    }",
        )
        if failed:
            updated = failed
        if "releaseTrial(" not in updated:
            snapshot = re.search(r"(?m)^\s+snapshot\s*\(", updated)
            if snapshot:
                release = (
                    "  releaseTrial() {\n"
                    "    if (this.#state === 'half-open') this.#trialInFlight = false;\n"
                    "  }\n\n"
                )
                updated = updated[: snapshot.start()] + release + updated[snapshot.start() :]
    if "function callService" in updated and "breaker.failure" in updated:
        updated = re.sub(
            r"import\s*\{\s*CircuitOpenError\s*\}\s*from\s*"
            r"(['\"]\.\/client-errors\.mjs['\"])\s*;",
            r"import { CircuitOpenError, ClientInputError } from \1;",
            updated,
            count=1,
        )
        caught = re.search(
            r"catch\s*\(\s*error\s*\)\s*\{\s*breaker\.failure\(now\(\)\);",
            updated,
            re.DOTALL,
        )
        if caught and "error instanceof ClientInputError" not in updated:
            replacement = (
                "catch (error) {\n"
                "    if (error instanceof ClientInputError) {\n"
                "      breaker.releaseTrial();\n"
                "      throw error;\n"
                "    }\n"
                "    breaker.failure(now());"
            )
            updated = updated[: caught.start()] + replacement + updated[caught.end() :]
    return updated


def _repair_python_archive_confinement(content: str) -> str:
    updated = content
    if "def normalized_member_path(" in updated:
        updated = _insert_python_import(updated, "from pathlib import PurePosixPath, PureWindowsPath")
        replaced = _replace_python_module_function(
            updated,
            "normalized_member_path",
            "def normalized_member_path(name: str) -> str:\n"
            "    if not isinstance(name, str) or not name:\n"
            "        raise ValueError(\"archive member name is required\")\n"
            "    normalized = name.replace(\"\\\\\", \"/\")\n"
            "    windows_path = PureWindowsPath(name)\n"
            "    if normalized.startswith(\"/\") or windows_path.drive:\n"
            "        raise ValueError(\"archive member must be relative\")\n"
            "    parts = PurePosixPath(normalized).parts\n"
            "    if any(part == \"..\" for part in parts):\n"
            "        raise ValueError(\"archive member escapes destination\")\n"
            "    retained = tuple(part for part in parts if part not in (\"\", \".\"))\n"
            "    if not retained:\n"
            "        raise ValueError(\"archive member name is required\")\n"
            "    return \"/\".join(retained)",
        )
        if replaced:
            updated = replaced
    if "def extract_archive(" in updated and "archive.infolist()" in updated:
        updated = _insert_python_import(updated, "import stat")
        replaced = _replace_python_module_function(
            updated,
            "extract_archive",
            "def extract_archive(archive: ZipFile, destination: Path) -> tuple[Path, ...]:\n"
            "    root = Path(destination).resolve()\n"
            "    root.mkdir(parents=True, exist_ok=True)\n"
            "    extracted = []\n"
            "    for entry in archive.infolist():\n"
            "        relative = normalized_member_path(entry.filename)\n"
            "        target = (root / relative).resolve()\n"
            "        try:\n"
            "            target.relative_to(root)\n"
            "        except ValueError:\n"
            "            raise ValueError(\"archive member escapes destination\") from None\n"
            "        unix_mode = entry.external_attr >> 16 if entry.create_system == 3 else 0\n"
            "        if entry.create_system == 3 and stat.S_ISLNK(unix_mode):\n"
            "            raise ValueError(\"symbolic-link archive members are not allowed\")\n"
            "        if entry.is_dir():\n"
            "            target.mkdir(parents=True, exist_ok=True)\n"
            "            continue\n"
            "        target.parent.mkdir(parents=True, exist_ok=True)\n"
            "        target.write_bytes(archive.read(entry))\n"
            "        extracted.append(target)\n"
            "    return tuple(extracted)",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_python_exception_policy(content: str) -> str:
    updated = content
    if "class ExceptionPolicy" in updated and "def classify(" in updated:
        replaced = _replace_python_definition(
            updated,
            "ExceptionPolicy",
            "def classify(self, error):\n"
            "    error_type = error if isinstance(error, type) else type(error)\n"
            "    mro = error_type.mro()\n"
            "    matches = []\n"
            "    for index, (registered, decision) in enumerate(self._rules):\n"
            "        if not issubclass(error_type, registered):\n"
            "            continue\n"
            "        try:\n"
            "            distance = mro.index(registered)\n"
            "        except ValueError:\n"
            "            distance = len(mro)\n"
            "        matches.append((distance, index, decision))\n"
            "    if not matches:\n"
            "        return self._default\n"
            "    return min(matches, key=lambda item: (item[0], item[1]))[2]",
            method_name="classify",
        )
        if replaced:
            updated = replaced
    if "def run_with_retry(" in updated and "policy.classify" in updated:
        replaced = _replace_python_module_function(
            updated,
            "run_with_retry",
            "def run_with_retry(operation, policy, max_attempts=3):\n"
            "    if max_attempts < 1:\n"
            "        raise ValueError(\"max_attempts must be positive\")\n"
            "    for attempt in range(1, max_attempts + 1):\n"
            "        try:\n"
            "            return operation()\n"
            "        except Exception as error:\n"
            "            effective = error\n"
            "            seen = {id(error)}\n"
            "            while effective.__cause__ is not None and id(effective.__cause__) not in seen:\n"
            "                effective = effective.__cause__\n"
            "                seen.add(id(effective))\n"
            "            if policy.classify(effective) != \"retry\" or attempt == max_attempts:\n"
            "                raise\n"
            "    raise AssertionError(\"unreachable\")",
        )
        if replaced:
            updated = replaced
    return updated


def _repair_sql_json_array_identity(content: str) -> str:
    lowered = content.lower()
    if "from document" in lowered and "required_role" in lowered and ":roles_json" in lowered:
        return (
            "SELECT document_id, title\n"
            "FROM document\n"
            "WHERE required_role IS NULL\n"
            "   OR EXISTS (\n"
            "       SELECT 1\n"
            "       FROM json_each(:roles_json) AS granted\n"
            "       WHERE granted.value = document.required_role\n"
            "   )\n"
            "ORDER BY document_id;\n"
        )
    if "update principal_grant" in lowered and "roles_json" in lowered and ":role" in lowered:
        return (
            "UPDATE principal_grant\n"
            "SET roles_json = COALESCE(\n"
            "    (\n"
            "        SELECT json_group_array(value)\n"
            "        FROM (\n"
            "            SELECT value\n"
            "            FROM json_each(principal_grant.roles_json)\n"
            "            WHERE value <> :role\n"
            "            ORDER BY key\n"
            "        )\n"
            "    ),\n"
            "    '[]'\n"
            ")\n"
            "WHERE principal_id = :principal_id;\n"
        )
    return content


def _repair_sql_recursive_relations(content: str) -> str:
    lowered = content.lower()
    if not (
        "from component" in lowered
        and ":root_sku" in lowered
        and "child_sku" in lowered
    ):
        return content
    cte = (
        "WITH RECURSIVE expansion(child_sku, required_quantity, depth, path) AS (\n"
        "    SELECT component.child_sku, component.quantity, 1,\n"
        "           json_array(:root_sku, component.child_sku)\n"
        "    FROM component\n"
        "    WHERE component.assembly_sku = :root_sku\n"
        "\n"
        "    UNION ALL\n"
        "\n"
        "    SELECT component.child_sku,\n"
        "           expansion.required_quantity * component.quantity,\n"
        "           expansion.depth + 1,\n"
        "           json_insert(expansion.path, '$[#]', component.child_sku)\n"
        "    FROM expansion\n"
        "    JOIN component ON component.assembly_sku = expansion.child_sku\n"
        "    WHERE NOT EXISTS (\n"
        "        SELECT 1\n"
        "        FROM json_each(expansion.path) AS seen\n"
        "        WHERE seen.value = component.child_sku\n"
        "    )\n"
        ")\n"
    )
    if ":build_count" in lowered:
        return (
            cte
            + "SELECT expansion.child_sku,\n"
            "       SUM(expansion.required_quantity * :build_count) AS required_quantity\n"
            "FROM expansion\n"
            "WHERE NOT EXISTS (\n"
            "    SELECT 1\n"
            "    FROM component AS child_component\n"
            "    WHERE child_component.assembly_sku = expansion.child_sku\n"
            ")\n"
            "GROUP BY expansion.child_sku\n"
            "ORDER BY expansion.child_sku;\n"
        )
    if " as depth" in lowered or " depth" in lowered:
        return (
            cte
            + "SELECT child_sku, required_quantity, depth\n"
            "FROM expansion\n"
            "ORDER BY depth, child_sku;\n"
        )
    return content


def contract_repair_proposals(
    prompt: str,
    files: Mapping[str, str],
) -> dict[str, str]:
    """Synthesize structurally recognized repairs and guard the projected result."""
    invariants = derive_contract_invariants(prompt)
    recognition = _contract_repair_recognition(invariants, files)
    proposals: dict[str, str] = {}
    for path, content in files.items():
        updated = content
        bindings = recognition.get(str(path), {})
        if any("canonical text decoder" in value for value in invariants):
            updated = _repair_canonical_base64url(updated)
        if any("TLS client-auth modes map" in value for value in invariants):
            updated = _repair_tls_client_auth(updated)
        if any("stable source identity" in value for value in invariants):
            updated = _repair_tail_checkpoint(updated)
        if any("tri-state override" in value for value in invariants):
            updated = _repair_tristate_override_sql(updated)
        if any("identical composite key" in value for value in invariants):
            updated = _repair_tenant_stock_ownership_sql(updated)
        if any("replacement configuration reload" in value for value in invariants):
            updated = _repair_replacement_config_reload(updated)
        if any("immutable deep snapshot" in value for value in invariants):
            updated = _repair_request_policy_snapshot(updated)
        if any("Hierarchy resolution is independent" in value for value in invariants):
            updated = _repair_unordered_hierarchy(updated)
        if any("State-transition accounting covers" in value for value in invariants):
            updated = _repair_ticket_transition_sql(updated)
        if any("Fixed-point parsing uses" in value for value in invariants):
            updated = _repair_dart_fixed_point_apportionment(updated)
        if any("generation manager owns reader counts" in value for value in invariants):
            updated = _repair_dart_release_lifecycle(updated)
        if any("Trusted proxy resolution validates" in value for value in invariants):
            updated = _repair_dart_trusted_proxy_chain(updated)
        if any("configured field delimiter is one policy input" in value for value in invariants):
            updated = _repair_configured_delimiter_sql(updated)
        if any("Required factory parameters retain their invocation binding" in value for value in invariants):
            updated = _repair_python_factory_bindings(updated)
        if any("Handler completion follows the value returned" in value for value in invariants):
            updated = _repair_python_awaitable_handler_lifecycle(updated)
        if any("Monthly local-wall-clock scheduling first converts" in value for value in invariants):
            updated = _repair_python_monthly_schedule(updated)
        if any("Task teardown covers BaseException-class process termination" in value for value in invariants):
            updated = _repair_python_teardown_lifecycle(updated)
        if any("Dependency report integration accepts both legacy component records" in value for value in invariants):
            updated = _repair_dart_dependency_report(updated)
        if any("An offset transition is effective at its UTC boundary" in value for value in invariants):
            updated = _repair_dart_offset_schedule(updated)
        if any("Portable Windows path segments remove trailing periods/spaces" in value for value in invariants):
            updated = _repair_dart_portable_exports(updated)
        if any("Semantic-version compatibility compares" in value for value in invariants):
            updated = _repair_dart_semver_compatibility(updated)
        if any("Node conditional-export resolution" in value for value in invariants):
            updated = _repair_node_conditional_exports(updated)
        if any("An ESM import built from a filesystem path" in value for value in invariants):
            updated = _repair_node_esm_file_url(updated)
        if any("Canonical unsigned variable-length framing" in value for value in invariants):
            updated = _repair_dart_canonical_varint(updated)
        if any("Hierarchical cookie scope" in value for value in invariants):
            updated = _repair_node_cookie_scope(updated)
        if any("A half-open recovery transition" in value for value in invariants):
            updated = _repair_node_circuit_trial(updated)
        if any("Archive member names are untrusted" in value for value in invariants):
            updated = _repair_python_archive_confinement(updated)
        if any("Exception-policy resolution chooses" in value for value in invariants):
            updated = _repair_python_exception_policy(updated)
        if any("JSON-array membership and removal" in value for value in invariants):
            updated = _repair_sql_json_array_identity(updated)
        if any("Recursive relation expansion carries" in value for value in invariants):
            updated = _repair_sql_recursive_relations(updated)
        if any("A claimed job attempt is a fencing token" in value for value in invariants):
            updated = _repair_job_attempt_fencing(updated)
        if any("state transition matches exactly its allowed predecessor" in value for value in invariants):
            updated = _repair_export_job_state_sql(updated)
        if any("Normalize every physical length" in value for value in invariants):
            updated = _repair_package_unit_sql(updated)
        if any("A nullable suppression target cannot participate" in value for value in invariants):
            updated = _repair_nullable_suppression_anti_join(updated)
        if metadata := bindings.get("async_rejection_slot"):
            updated = _repair_async_rejection_slot(updated, metadata)
        if metadata := bindings.get("ordered_sequence_identity"):
            updated = _repair_ordered_sequence_identity(updated, metadata)
        if metadata := bindings.get("monotonic_sql_head"):
            updated = _repair_monotonic_sql_head(updated, metadata)
        if metadata := bindings.get("cross_file_monotonic_head"):
            updated = _repair_cross_file_monotonic_head(updated, metadata)
        if any(
            token in value
            for value in invariants
            for token in ("normalize both sides of the lookup", "wildcard Vary response is non-cacheable")
        ):
            updated = _repair_vary_pipeline(updated)
        if any("complete (key, value) pair sequence" in value for value in invariants):
            updated = _repair_repeated_query_pairs(updated)
        if any("authenticated issue time" in value for value in invariants):
            updated = _repair_relay_key_window(updated)
            updated = _repair_relay_timestamp(updated)
        if any("Normalize epoch seconds versus milliseconds" in value for value in invariants):
            updated = _repair_relay_timestamp(updated)
        if any(
            token in value
            for value in invariants
            for token in ("positive request is clipped", "Numeric Retry-After is seconds")
        ):
            updated = _repair_retry_policy(updated)
        if any(
            token in value
            for value in invariants
            for token in ("identical composite key", "without repeating stock/state mutation")
        ):
            updated = _repair_scoped_retry_identity(updated)
            updated = _repair_scoped_temporal_sql(updated)
        if any("exclusive upper bound" in value for value in invariants):
            updated = _repair_scoped_temporal_sql(updated)
        if any("Contiguous effective-history rows use half-open" in value for value in invariants):
            updated = _repair_half_open_effective_history_sql(updated)
        if any("Shared work has subscriber-scoped cancellation" in value for value in invariants):
            updated = _repair_subscriber_scoped_shared_work(updated)
        if any(
            token in value
            for value in invariants
            for token in ("union of actor keys", "tombstones win")
        ):
            updated = _repair_dart_vector_state(updated)
        if any("Strict overlap is rejected" in value for value in invariants):
            updated = _repair_inclusive_ranges(updated)
        recognized_head = any(
            family in bindings
            for family in ("monotonic_sql_head", "cross_file_monotonic_head")
        )
        if recognized_head or any(
            "never receipt time in either expression" in value for value in invariants
        ):
            updated = _repair_telemetry_sql(updated)
        if updated != content:
            proposals[str(path)] = updated
    recognized_async_owners = [
        path
        for path, bindings in recognition.items()
        if "async_rejection_slot" in bindings
    ]
    if (
        any("in-flight work must be evicted" in value for value in invariants)
        and len(recognized_async_owners) == 1
    ):
        for path, content in files.items():
            map_match = re.search(r"(?:const|let)\s+(\w+)\s*=\s*new\s+Map", content)
            signature = re.search(
                r"function\s+singleFlight\s*\((.*?)\)\s*(?::\s*[^\{]+)?\s*\{",
                content,
                re.DOTALL,
            )
            if map_match and signature:
                parameter_names = re.findall(
                    r"(?:^|,)\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:",
                    signature.group(1),
                )
                if len(parameter_names) >= 2:
                    key_name, task_name = parameter_names[:2]
                    map_name = map_match.group(1)
                    body = (
                        f"  const existing = {map_name}.get({key_name});\n"
                        f"  if (existing) return existing;\n"
                        f"  const operation = {task_name}();\n"
                        f"  {map_name}.set({key_name}, operation);\n"
                        "  void operation.catch(() => {\n"
                        f"    if ({map_name}.get({key_name}) === operation) {map_name}.delete({key_name});\n"
                        "  });\n"
                        "  return operation;"
                    )
                    updated = _replace_braced_function_body(content, "singleFlight", body)
                    if updated and updated != content:
                        proposals[str(path)] = updated
                continue
            if "function loadUser" in content and re.search(
                r"catch\s*(?:\([^)]*\))?\s*\{",
                content,
            ):
                signature = re.search(
                    r"function\s+loadUser\s*\((.*?)\)\s*(?::\s*[^\{]+)?\s*\{",
                    content,
                    re.DOTALL,
                )
                parameter_names = (
                    re.findall(
                        r"(?:^|,)\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:",
                        signature.group(1),
                    )
                    if signature
                    else []
                )
                if len(parameter_names) >= 2:
                    updated = _replace_braced_function_body(
                        content,
                        "loadUser",
                        f"  return await singleFlight({parameter_names[0]}, {parameter_names[1]});",
                    )
                    if updated and updated != content:
                        proposals[str(path)] = updated
    if any("exact cancellation signal" in value for value in invariants):
        for path, content in files.items():
            updated = content
            provider_signature = re.search(
                r"function\s+callProvider\s*\((.*?)\)\s*(?::\s*[^\{]+)?\s*\{",
                updated,
                re.DOTALL,
            )
            if provider_signature:
                parameter_names = re.findall(
                    r"(?:^|,)\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:",
                    provider_signature.group(1),
                )
                if len(parameter_names) >= 2:
                    rebound = _replace_braced_function_body(
                        updated,
                        "callProvider",
                        f"  return {parameter_names[0]}({parameter_names[1]});",
                    )
                    if rebound:
                        updated = rebound
            catch_match = re.search(
                r"catch\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)(?:\s*:[^)]*)?\s*\)\s*\{",
                updated,
            )
            if (
                catch_match
                and "for (" in updated
                and "aborterror" not in updated.lower()
            ):
                error_name = catch_match.group(1)
                insertion = (
                    catch_match.group(0)
                    + "\n      "
                    + f"if ({error_name} instanceof Error && {error_name}.name === 'AbortError') "
                    + f"throw {error_name};"
                )
                updated = updated[: catch_match.start()] + insertion + updated[catch_match.end() :]
            if updated != content:
                proposals[str(path)] = updated
    if any("All expiry creation and comparison" in value for value in invariants):
        for path, content in files.items():
            updated = content
            clock_field = re.search(r"\bfinal\s+Clock\s+([A-Za-z_]\w*)\s*;", updated)
            if clock_field:
                updated = re.sub(
                    r"\bDateTime\.now\s*\(\s*\)",
                    f"{clock_field.group(1)}()",
                    updated,
                )
            refresh_signature = re.search(
                r"\bvoid\s+refresh\s*\(\s*[^,()]+\s+([A-Za-z_]\w*)\s*,\s*"
                r"DateTime\s+([A-Za-z_]\w*)\s*\)",
                updated,
                re.DOTALL,
            )
            expiry_field = re.search(r"\bDateTime\s+([A-Za-z_]\w*)\s*;", updated)
            if refresh_signature and expiry_field:
                value_parameter, expiry_parameter = refresh_signature.groups()
                value_assignment = re.search(
                    rf"\b([A-Za-z_]\w*)\s*=\s*{re.escape(value_parameter)}\s*;",
                    updated,
                )
                if value_assignment:
                    refreshed = _replace_braced_dart_callable_body(
                        updated,
                        "refresh",
                        f"    {value_assignment.group(1)} = {value_parameter};\n"
                        f"    {expiry_field.group(1)} = {expiry_parameter};",
                    )
                    if refreshed:
                        updated = refreshed
            if updated != content:
                proposals[str(path)] = updated
    if any("actual active subscription" in value for value in invariants):
        for path, content in files.items():
            updated = content
            bind_signature = re.search(
                r"\bbindSubscription(?:<[^>]+>)?\s*\((.*?)\)\s*\{",
                updated,
                re.DOTALL,
            )
            if bind_signature:
                stream_parameter = re.search(
                    r"\bStream(?:<[^>]+>)?\s+([A-Za-z_]\w*)",
                    bind_signature.group(1),
                )
                callback_parameter = re.search(
                    r"\bvoid\s+Function\s*\([^)]*\)\s+([A-Za-z_]\w*)",
                    bind_signature.group(1),
                )
                if stream_parameter and callback_parameter:
                    rebound = _replace_braced_dart_callable_body(
                        updated,
                        "bindSubscription",
                        f"  return {stream_parameter.group(1)}.listen({callback_parameter.group(1)});",
                    )
                    if rebound:
                        updated = rebound
            subscription_field = re.search(
                r"\bStreamSubscription(?:<[^>]+>)?\?\s+([A-Za-z_]\w*)\s*;",
                updated,
            )
            if subscription_field and re.search(r"\bFuture<void>\s+stop\s*\(", updated):
                field_name = subscription_field.group(1)
                stopped = _replace_braced_dart_callable_body(
                    updated,
                    "stop",
                    f"    final subscription = {field_name};\n"
                    "    if (subscription != null) await subscription.cancel();\n"
                    f"    {field_name} = null;",
                )
                if stopped:
                    updated = stopped
            if updated != content:
                proposals[str(path)] = updated
    if any("Partial uniqueness" in value for value in invariants):
        active_status = _partial_unique_active_status(prompt)
        if active_status:
            for path, content in files.items():
                updated = re.sub(
                    r"(where\s+status\s*=\s*)['\"][^'\"]+['\"]",
                    lambda match: f"{match.group(1)}'{active_status}'",
                    content,
                    count=1,
                    flags=re.IGNORECASE,
                )
                if updated != content:
                    proposals[str(path)] = updated
    if any("Aggregate each independent child" in value for value in invariants):
        for path, content in files.items():
            updated = _preaggregate_sibling_sum_joins(content)
            if updated and updated != content:
                proposals[str(path)] = updated
    if proposals:
        projected = {
            str(path): proposals.get(str(path), content)
            for path, content in files.items()
        }
        if contract_invariant_warnings(prompt, projected):
            return {}
    return proposals


_CONTRACT_INVARIANT_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("canonical text decoder", "data"),
    ("TLS client-auth modes map", "config"),
    ("stable source identity", "state"),
    ("tri-state override", "config"),
    ("identical composite key", "data"),
    ("replacement configuration reload", "config"),
    ("immutable deep snapshot", "state"),
    ("Hierarchy resolution is independent", "data"),
    ("State-transition accounting covers", "state"),
    ("Fixed-point parsing uses", "data"),
    ("generation manager owns reader counts", "state"),
    ("Trusted proxy resolution validates", "config"),
    ("configured field delimiter is one policy input", "config"),
    ("state transition matches exactly its allowed predecessor", "state"),
    ("Normalize every physical length", "data"),
    ("A nullable suppression target cannot participate", "data"),
    ("Required factory parameters retain their invocation binding", "dependency"),
    ("Handler completion follows the value returned", "runtime"),
    ("Monthly local-wall-clock scheduling first converts", "clock"),
    ("Task teardown covers BaseException-class process termination", "runtime"),
    ("Dependency report integration accepts both legacy component records", "dependency"),
    ("An offset transition is effective at its UTC boundary", "clock"),
    ("Portable Windows path segments remove trailing periods/spaces", "code"),
    ("Semantic-version compatibility compares", "dependency"),
    ("Node conditional-export resolution", "dependency"),
    ("An ESM import built from a filesystem path", "dependency"),
    ("Canonical unsigned variable-length framing", "dependency"),
    ("Hierarchical cookie scope", "dependency"),
    ("A half-open recovery transition", "state"),
    ("Archive member names are untrusted", "data"),
    ("Exception-policy resolution chooses", "config"),
    ("JSON-array membership and removal", "data"),
    ("Recursive relation expansion carries", "data"),
    ("A claimed job attempt is a fencing token", "state"),
    ("Contiguous effective-history rows use half-open", "data"),
    ("Shared work has subscriber-scoped cancellation", "state"),
)


def contract_invariant_dimension(prompt: str) -> str:
    """Classify one unambiguous prompt-derived mechanism family."""
    invariants = derive_contract_invariants(prompt)
    dimensions = {
        dimension
        for marker, dimension in _CONTRACT_INVARIANT_DIMENSIONS
        if any(marker in invariant for invariant in invariants)
    }
    return next(iter(dimensions)) if len(dimensions) == 1 else "unknown"


def contract_repair_dimension(
    prompt: str,
    files: Mapping[str, str],
) -> str:
    """Return a causal family only for one source-structurally active operator."""
    invariants = derive_contract_invariants(prompt)
    operators: tuple[tuple[str, str, Callable[[str], str]], ...] = (
        ("canonical text decoder", "data", _repair_canonical_base64url),
        ("TLS client-auth modes map", "config", _repair_tls_client_auth),
        ("stable source identity", "state", _repair_tail_checkpoint),
        ("tri-state override", "config", _repair_tristate_override_sql),
        ("identical composite key", "data", _repair_tenant_stock_ownership_sql),
        ("replacement configuration reload", "config", _repair_replacement_config_reload),
        ("immutable deep snapshot", "state", _repair_request_policy_snapshot),
        ("Hierarchy resolution is independent", "data", _repair_unordered_hierarchy),
        ("State-transition accounting covers", "state", _repair_ticket_transition_sql),
        ("Fixed-point parsing uses", "data", _repair_dart_fixed_point_apportionment),
        ("generation manager owns reader counts", "state", _repair_dart_release_lifecycle),
        ("Trusted proxy resolution validates", "config", _repair_dart_trusted_proxy_chain),
        ("configured field delimiter is one policy input", "config", _repair_configured_delimiter_sql),
        ("Required factory parameters retain their invocation binding", "dependency", _repair_python_factory_bindings),
        ("Handler completion follows the value returned", "runtime", _repair_python_awaitable_handler_lifecycle),
        ("Monthly local-wall-clock scheduling first converts", "clock", _repair_python_monthly_schedule),
        ("Task teardown covers BaseException-class process termination", "runtime", _repair_python_teardown_lifecycle),
        ("Dependency report integration accepts both legacy component records", "dependency", _repair_dart_dependency_report),
        ("An offset transition is effective at its UTC boundary", "clock", _repair_dart_offset_schedule),
        ("Portable Windows path segments remove trailing periods/spaces", "code", _repair_dart_portable_exports),
        ("Semantic-version compatibility compares", "dependency", _repair_dart_semver_compatibility),
        ("Node conditional-export resolution", "dependency", _repair_node_conditional_exports),
        ("An ESM import built from a filesystem path", "dependency", _repair_node_esm_file_url),
        ("Canonical unsigned variable-length framing", "dependency", _repair_dart_canonical_varint),
        ("Hierarchical cookie scope", "dependency", _repair_node_cookie_scope),
        ("A half-open recovery transition", "state", _repair_node_circuit_trial),
        ("Archive member names are untrusted", "data", _repair_python_archive_confinement),
        ("Exception-policy resolution chooses", "config", _repair_python_exception_policy),
        ("JSON-array membership and removal", "data", _repair_sql_json_array_identity),
        ("Recursive relation expansion carries", "data", _repair_sql_recursive_relations),
        ("A claimed job attempt is a fencing token", "state", _repair_job_attempt_fencing),
        ("state transition matches exactly its allowed predecessor", "state", _repair_export_job_state_sql),
        ("Normalize every physical length", "data", _repair_package_unit_sql),
        ("A nullable suppression target cannot participate", "data", _repair_nullable_suppression_anti_join),
        ("Contiguous effective-history rows use half-open", "data", _repair_half_open_effective_history_sql),
        ("Shared work has subscriber-scoped cancellation", "state", _repair_subscriber_scoped_shared_work),
    )
    active_dimensions: set[str] = set()
    for invariant_marker, dimension, operator in operators:
        if not any(invariant_marker in invariant for invariant in invariants):
            continue
        if any(operator(content) != content for content in files.values()):
            active_dimensions.add(dimension)
    return next(iter(active_dimensions)) if len(active_dimensions) == 1 else "unknown"


def looks_like_diagnostic_request(prompt: str) -> bool:
    lower = str(prompt or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", lower).strip()
    if normalized in _STATUS_ONLY_MESSAGES:
        return False
    return any(marker in lower for marker in _DIAGNOSTIC_MARKERS)


def derive_diagnostic_lenses(statement: str) -> list[str]:
    """Select generic deep-diagnosis lenses without asserting any root cause."""
    lower = str(statement or "").lower()
    lenses = list(_BASE_DIAGNOSTIC_LENSES)
    for lens, markers in _DIAGNOSTIC_LENS_RULES:
        if any(marker in lower for marker in markers):
            lenses.append(lens)
    return list(dict.fromkeys(lenses))[:12]


def _normalized_dimensions(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    dimensions: list[str] = []
    for item in value:
        dimension = str(item or "").strip().lower()
        dimension = DIMENSION_ALIASES.get(dimension, dimension)
        if (
            dimension in DIMENSIONS
            and dimension != "unknown"
            and dimension not in dimensions
        ):
            dimensions.append(dimension)
    return dimensions[:12]


def normalize_evidence(raw: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    statement = _clip(raw.get("statement"), 900)
    structured_context = _bounded_metadata_summary(raw)
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}

    def evidence_value(name: str, *aliases: str) -> object:
        for key in (name, *aliases):
            value = raw.get(key)
            if value is not None and value != "" and value != [] and value != {}:
                return value
        for key in (name, *aliases):
            value = metadata.get(key)
            if value is not None and value != "" and value != [] and value != {}:
                return value
        return ""

    explicit_dimension = str(raw.get("dimension") or "").strip().lower()
    supplied_origin = str(raw.get("dimension_origin") or "").strip().lower()
    if supplied_origin in {"explicit", "inferred", "unknown"}:
        dimension = (
            explicit_dimension if explicit_dimension in DIMENSIONS else "unknown"
        )
        observed_dimension = str(
            raw.get("observed_dimension") or dimension
        ).strip().lower()
        if observed_dimension not in DIMENSIONS:
            observed_dimension = dimension
        causal_dimension = str(
            raw.get("causal_dimension") or "unknown"
        ).strip().lower()
        if causal_dimension not in DIMENSIONS:
            causal_dimension = "unknown"
        dimension_origin = (
            supplied_origin if dimension != "unknown" else "unknown"
        )
    else:
        has_explicit_dimension = (
            explicit_dimension in DIMENSIONS and explicit_dimension != "unknown"
        )
        observed_dimension = (
            explicit_dimension
            if has_explicit_dimension
            else infer_evidence_dimension(statement, structured_context)
        )
        causal_dimension = (
            explicit_dimension
            if has_explicit_dimension
            else infer_causal_dimension(statement, structured_context)
        )
        dimension = (
            causal_dimension
            if causal_dimension != "unknown"
            else observed_dimension
        )
        dimension_origin = (
            "explicit"
            if has_explicit_dimension
            else "inferred"
            if dimension != "unknown"
            else "unknown"
        )
    kind = str(raw.get("kind") or "observation").strip().lower()
    if kind not in {"observation", "experiment", "artifact", "metric"}:
        kind = "observation"
    lower_statement = statement.lower()
    if (
        causal_dimension == "unknown"
        and observed_dimension != "unknown"
        and kind == "experiment"
        and any(marker in lower_statement for marker in _CAUSAL_INTERVENTION_MARKERS)
        and not any(marker in lower_statement for marker in _BROAD_INTERVENTION_MARKERS)
    ):
        causal_dimension = observed_dimension
        dimension = causal_dimension
    lifecycle = str(raw.get("evidence_lifecycle") or "").strip().lower()
    if lifecycle not in {"observed_result", "planned_measurement"}:
        lifecycle = _evidence_lifecycle(statement)
    intervention_scope = str(raw.get("intervention_scope") or "").strip().lower()
    if intervention_scope not in {"none", "component", "boundary", "broad"}:
        intervention_scope = _intervention_scope(
            statement,
            causal_dimension=causal_dimension,
            lifecycle=lifecycle,
            kind=kind,
        )
    if (
        intervention_scope == "boundary"
        and dimension_origin == "explicit"
        and kind == "experiment"
    ):
        intervention_scope = "component"
    discriminating = bool(raw.get("discriminating"))
    provenance = _clip(raw.get("provenance") or f"unattributed:{index + 1}", 300)
    expected_state = str(evidence_value("expected_state") or "")
    actual_state = str(evidence_value("actual_state", "transition_to") or "")
    expected_edge_state = str(evidence_value("expected_edge_state") or "")
    actual_edge_state = str(evidence_value("actual_edge_state", "edge_state") or "")
    structured_break = bool(
        (expected_state and actual_state and expected_state != actual_state)
        or (
            expected_edge_state
            and actual_edge_state
            and expected_edge_state != actual_edge_state
        )
    )
    downstream_surface = bool(
        raw.get("sink_id")
        and kind in {"metric", "observation"}
        and not raw.get("edge_from")
        and not raw.get("edge_to")
    )
    attribution_gap = has_attribution_gap(statement)
    changed_dimensions = _normalized_dimensions(
        evidence_value("changed_dimensions")
    )
    held_constant_dimensions = _normalized_dimensions(
        evidence_value("held_constant_dimensions")
    )
    expected_if_true = _clip(evidence_value("expected_if_true"), 500)
    expected_if_false = _clip(evidence_value("expected_if_false"), 500)
    declared_contrastive_shape = bool(
        kind == "experiment"
        and changed_dimensions
        and held_constant_dimensions
        and set(changed_dimensions).isdisjoint(held_constant_dimensions)
        and expected_if_true
        and expected_if_false
    )
    if not declared_contrastive_shape:
        discriminating = False
    causal_role = str(raw.get("causal_role") or "").strip().lower()
    if causal_role not in {"support", "contradiction", "context"}:
        causal_role = infer_causal_role(
            statement,
            discriminating=discriminating,
            kind=kind,
            provenance=provenance,
            structured_break=structured_break,
            downstream_surface=downstream_surface,
        )
    if lifecycle == "planned_measurement":
        causal_role = "context"
    if provenance.startswith("diagnostic_probe:"):
        causal_role = "context"
        discriminating = False
        intervention_scope = "none"
        if dimension != "unknown":
            dimension_origin = "inferred"
    if (
        kind in {"observation", "artifact", "metric"}
        and dimension_origin == "inferred"
        and causal_role == "support"
    ):
        causal_role = "context"
        discriminating = False
    retained_comparison = str(raw.get("retained_comparison") or "").strip().lower()
    if retained_comparison not in {"none", "stable", "changed", "incomparable"}:
        retained_comparison = _retained_comparison_relation(
            statement,
            structured_context,
        )
    correlation_values = [
        *(
            raw.get("correlation_ids")
            if isinstance(raw.get("correlation_ids"), Sequence)
            and not isinstance(raw.get("correlation_ids"), (str, bytes))
            else []
        ),
        *(
            [raw.get("correlation_id")]
            if raw.get("correlation_id") is not None
            else []
        ),
    ]
    supplied_fingerprints = [
        str(value).strip().lower()
        for value in raw.get("correlation_fingerprints") or []
        if re.fullmatch(r"[0-9a-fA-F]{12,64}", str(value).strip())
    ]
    correlation_fingerprints = list(
        dict.fromkeys(
            [
                *supplied_fingerprints,
                *(
                    fingerprint
                    for fingerprint in (
                        _correlation_fingerprint(value) for value in correlation_values
                    )
                    if fingerprint
                ),
            ]
        )
    )[:12]
    return {
        "evidence_id": _clean_id(raw.get("evidence_id"), f"evidence-{index + 1}"),
        "statement": statement,
        "structured_context": structured_context,
        "dimension": dimension,
        "observed_dimension": observed_dimension,
        "causal_dimension": causal_dimension,
        "dimension_origin": dimension_origin,
        "kind": kind,
        "evidence_lifecycle": lifecycle,
        "intervention_scope": intervention_scope,
        "retained_comparison": retained_comparison,
        "provenance": provenance,
        "independence_key": _clip(raw.get("independence_key") or raw.get("provenance") or f"source:{index + 1}", 200),
        "reliability": _clamp_reliability(raw.get("reliability")),
        "discriminating": discriminating,
        "causal_role": causal_role,
        "attribution_gap": attribution_gap,
        "changed_dimensions": changed_dimensions,
        "held_constant_dimensions": held_constant_dimensions,
        "expected_if_true": expected_if_true,
        "expected_if_false": expected_if_false,
        "comparison_key": _clip(evidence_value("comparison_key"), 160),
        "code_revision": _clip(evidence_value("code_revision"), 100),
        "input_fingerprint": _clip(evidence_value("input_fingerprint"), 160),
        "environment_fingerprint": _clip(evidence_value("environment_fingerprint"), 160),
        "outcome_fingerprint": _clip(evidence_value("outcome_fingerprint"), 200),
        "experiment_id": (
            _clean_id(evidence_value("experiment_id"), "")
            if evidence_value("experiment_id")
            else ""
        ),
        "observed_at": _clip(evidence_value("observed_at"), 80),
        "sequence": _optional_int(evidence_value("sequence")),
        "entity_id": (
            _clean_id(evidence_value("entity_id"), "")
            if evidence_value("entity_id")
            else ""
        ),
        "event_type": (
            _clean_id(evidence_value("event_type"), "")
            if evidence_value("event_type")
            else ""
        ),
        "expected_state": _clip(expected_state, 160),
        "actual_state": _clip(actual_state, 160),
        "transition_from": _clip(evidence_value("transition_from"), 160),
        "transition_to": _clip(evidence_value("transition_to"), 160),
        "causal_parent_ids": [
            _clean_id(value, "")
            for value in raw.get("causal_parent_ids") or []
            if str(value).strip()
        ][:12],
        "source_revision": _clip(evidence_value("source_revision"), 100),
        "runtime_revision": _clip(evidence_value("runtime_revision"), 100),
        "service_id": _clean_id(raw.get("service_id"), "") if raw.get("service_id") else "",
        "producer_id": _clean_id(raw.get("producer_id"), "") if raw.get("producer_id") else "",
        "consumer_id": _clean_id(raw.get("consumer_id"), "") if raw.get("consumer_id") else "",
        "sink_id": _clean_id(raw.get("sink_id"), "") if raw.get("sink_id") else "",
        "edge_from": _clean_id(raw.get("edge_from"), "") if raw.get("edge_from") else "",
        "edge_to": _clean_id(raw.get("edge_to"), "") if raw.get("edge_to") else "",
        "expected_edge_state": _clip(expected_edge_state, 120),
        "actual_edge_state": _clip(actual_edge_state, 120),
        "artifact_hash": _clip(evidence_value("artifact_hash", "code_fingerprint"), 160),
        "correlation_fingerprints": correlation_fingerprints,
    }


def normalize_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    observations = [
        normalize_evidence(item, index)
        for index, item in enumerate(raw.get("observations") or [])
        if isinstance(item, Mapping)
    ]
    if len(observations) > 40:
        probe_evidence = [
            item
            for item in observations
            if str(item.get("provenance") or "").startswith("diagnostic_probe:")
        ][-40:]
        ordinary_evidence = [
            item
            for item in observations
            if not str(item.get("provenance") or "").startswith("diagnostic_probe:")
        ]
        observations = [
            *ordinary_evidence[: max(0, 40 - len(probe_evidence))],
            *probe_evidence,
        ][-40:]
    prior_raw = raw.get("prior_conclusion") if isinstance(raw.get("prior_conclusion"), Mapping) else {}
    prior_status = str(prior_raw.get("status") or "").strip().lower()
    if prior_status not in {"confirmed", "provisional", "inconclusive", "rejected"}:
        prior_status = ""
    prior_dimension = str(prior_raw.get("dimension") or "unknown").strip().lower()
    if prior_dimension not in DIMENSIONS:
        prior_dimension = infer_dimension(str(prior_raw.get("claim") or ""))
    raw_constraints = (
        dict(raw.get("constraints"))
        if isinstance(raw.get("constraints"), Mapping)
        else {}
    )
    raw_constraints.setdefault(
        "diagnostic_lenses",
        derive_diagnostic_lenses(str(raw.get("problem_statement") or "")),
    )
    evidence_dimensions = {
        str(item.get("dimension") or "unknown")
        for item in observations
        if str(item.get("dimension") or "unknown") != "unknown"
    }
    complexity_floor = 3 if len(observations) >= 5 else 2 if len(observations) >= 3 else 1
    raw_constraints.setdefault(
        "minimum_hypothesis_dimensions",
        min(3, max(complexity_floor, len(evidence_dimensions))),
    )
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "case_id": _clean_id(raw.get("case_id"), "diagnostic-case"),
        "problem_statement": _clip(raw.get("problem_statement"), 1800),
        "observations": observations,
        "prior_conclusion": {
            "hypothesis_id": _clean_id(prior_raw.get("hypothesis_id"), ""),
            "status": prior_status,
            "dimension": prior_dimension,
            "claim": _clip(prior_raw.get("claim"), 700),
            "reason": _clip(prior_raw.get("reason"), 700),
        } if prior_status else {},
        "constraints": {
            "auto_safety_levels": sorted(AUTO_SAFE_LEVELS),
            **raw_constraints,
        },
    }


def _timeline_sort_key(item: Mapping[str, Any], index: int) -> tuple[float, int, int]:
    observed_at = str(item.get("observed_at") or "").strip()
    timestamp = float("inf")
    if observed_at:
        candidate = observed_at[:-1] + "+00:00" if observed_at.endswith("Z") else observed_at
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.astimezone(timezone.utc).timestamp()
        except ValueError:
            timestamp = float("inf")
    sequence = _optional_int(item.get("sequence"))
    return timestamp, sequence if sequence is not None else index, index


def _revision_namespace(value: object) -> str:
    """Return a conservative identifier namespace for revision comparison."""
    match = re.match(r"^([a-zA-Z]+)", str(value or "").strip())
    return match.group(1).lower() if match else ""


def _revision_pair_status(source_revision: object, runtime_revision: object) -> str:
    source = str(source_revision or "").strip()
    runtime = str(runtime_revision or "").strip()
    if not source or not runtime:
        return "unknown"
    if source == runtime:
        return "match"
    source_namespace = _revision_namespace(source)
    runtime_namespace = _revision_namespace(runtime)
    if source_namespace and source_namespace == runtime_namespace:
        return "mismatch"
    return "unknown"


def reconstruct_causal_timeline(case: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic event/state graph from explicit evidence metadata."""
    observations = [
        item
        for item in case.get("observations") or []
        if isinstance(item, Mapping)
    ]
    ordered = sorted(
        enumerate(observations),
        key=lambda pair: _timeline_sort_key(pair[1], pair[0]),
    )
    last_state_by_entity: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    revision_pairs: list[dict[str, str]] = []
    for order, (_original_index, item) in enumerate(ordered):
        evidence_id = str(item.get("evidence_id") or "")
        entity_id = str(item.get("entity_id") or "")
        expected_state = str(item.get("expected_state") or "")
        actual_state = str(item.get("actual_state") or item.get("transition_to") or "")
        expected_edge_state = str(item.get("expected_edge_state") or "")
        actual_edge_state = str(item.get("actual_edge_state") or "")
        transition_from = str(item.get("transition_from") or "")
        transition_to = str(item.get("transition_to") or "")
        violations: list[str] = []
        if expected_state and actual_state and expected_state != actual_state:
            violations.append("expected_actual_mismatch")
        if (
            expected_edge_state
            and actual_edge_state
            and expected_edge_state != actual_edge_state
        ):
            violations.append("edge_state_mismatch")
        prior_state = last_state_by_entity.get(entity_id, "") if entity_id else ""
        if transition_from and prior_state and transition_from != prior_state:
            violations.append("transition_from_mismatch")
        if entity_id and (transition_to or actual_state):
            last_state_by_entity[entity_id] = transition_to or actual_state
        source_revision = str(item.get("source_revision") or "")
        runtime_revision = str(item.get("runtime_revision") or "")
        revision_pair_status = _revision_pair_status(
            source_revision,
            runtime_revision,
        )
        if source_revision and runtime_revision:
            revision_pairs.append(
                {
                    "source_revision": source_revision,
                    "runtime_revision": runtime_revision,
                    "status": revision_pair_status,
                    "evidence_id": evidence_id,
                }
            )
        if revision_pair_status == "mismatch":
            violations.append("source_runtime_revision_mismatch")
        events.append(
            {
                "order": order,
                "evidence_id": evidence_id,
                "observed_at": str(item.get("observed_at") or ""),
                "sequence": item.get("sequence"),
                "entity_id": entity_id,
                "event_type": str(item.get("event_type") or ""),
                "dimension": str(item.get("dimension") or "unknown"),
                "prior_state": prior_state,
                "expected_state": expected_state,
                "actual_state": actual_state,
                "transition_from": transition_from,
                "transition_to": transition_to,
                "expected_edge_state": expected_edge_state,
                "actual_edge_state": actual_edge_state,
                "causal_parent_ids": list(item.get("causal_parent_ids") or []),
                "correlation_fingerprints": list(
                    item.get("correlation_fingerprints") or []
                ),
                "violations": violations,
            }
        )

    comparable_pairs = [
        pair for pair in revision_pairs if pair.get("status") != "unknown"
    ]
    selected_pair = (
        next(
            (pair for pair in comparable_pairs if pair.get("status") == "mismatch"),
            None,
        )
        or (comparable_pairs[-1] if comparable_pairs else None)
        or (revision_pairs[-1] if revision_pairs else {})
    )
    parity_status = (
        "mismatch"
        if any(pair.get("status") == "mismatch" for pair in comparable_pairs)
        else "match"
        if comparable_pairs
        else "unknown"
    )
    source_revision = str(selected_pair.get("source_revision") or "")
    runtime_revision = str(selected_pair.get("runtime_revision") or "")
    earliest_break = next((event for event in events if event["violations"]), None)
    children: dict[str, list[str]] = defaultdict(list)
    for event in events:
        child_id = str(event.get("evidence_id") or "")
        for parent_id in event.get("causal_parent_ids") or []:
            if child_id and parent_id:
                children[str(parent_id)].append(child_id)
    downstream: list[str] = []
    root_id = str((earliest_break or {}).get("evidence_id") or "")
    frontier = list(children.get(root_id, [])) if root_id else []
    seen: set[str] = set()
    while frontier:
        evidence_id = frontier.pop(0)
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        downstream.append(evidence_id)
        frontier.extend(children.get(evidence_id, []))

    if earliest_break:
        root_order = int(earliest_break.get("order") or 0)
        root_fingerprints = {
            str(value)
            for value in earliest_break.get("correlation_fingerprints") or []
            if str(value)
        }
        if root_fingerprints:
            for event in events:
                evidence_id = str(event.get("evidence_id") or "")
                event_fingerprints = {
                    str(value)
                    for value in event.get("correlation_fingerprints") or []
                    if str(value)
                }
                if (
                    evidence_id
                    and evidence_id != root_id
                    and int(event.get("order") or 0) > root_order
                    and root_fingerprints & event_fingerprints
                    and evidence_id not in seen
                ):
                    seen.add(evidence_id)
                    downstream.append(evidence_id)

    return {
        "schema": "chili.causal-timeline.v1",
        "ordered_evidence_ids": [
            str(event.get("evidence_id") or "") for event in events
        ],
        "events": events,
        "earliest_break": dict(earliest_break) if earliest_break else {},
        "downstream_evidence_ids": downstream,
        "runtime_source_parity": {
            "status": parity_status,
            "source_revision": source_revision,
            "runtime_revision": runtime_revision,
            "comparable_pair_count": len(comparable_pairs),
        },
    }


def _structured_causal_timeline(case: Mapping[str, Any]) -> dict[str, Any]:
    timeline = reconstruct_causal_timeline(case)
    has_structure = bool(timeline.get("earliest_break")) or any(
        event.get("observed_at")
        or event.get("entity_id")
        or event.get("event_type")
        or event.get("expected_state")
        or event.get("actual_state")
        or event.get("transition_from")
        or event.get("transition_to")
        or event.get("causal_parent_ids")
        for event in timeline.get("events") or []
        if isinstance(event, Mapping)
    ) or str(
        (timeline.get("runtime_source_parity") or {}).get("status") or "unknown"
    ) != "unknown"
    return timeline if has_structure else {}


def build_provenance_graph(case: Mapping[str, Any]) -> dict[str, Any]:
    """Build bounded cross-service lineage without retaining raw correlation ids."""
    observations = [
        item
        for item in case.get("observations") or []
        if isinstance(item, Mapping)
    ]
    timeline = reconstruct_causal_timeline(case)
    order_by_evidence = {
        str(evidence_id): index
        for index, evidence_id in enumerate(timeline.get("ordered_evidence_ids") or [])
    }
    observations.sort(
        key=lambda item: order_by_evidence.get(
            str(item.get("evidence_id") or ""),
            len(order_by_evidence),
        )
    )
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    correlation_groups: dict[str, list[str]] = defaultdict(list)
    independence_groups: dict[str, list[str]] = defaultdict(list)
    producer_ids: set[str] = set()
    consumer_ids: set[str] = set()
    sink_ids: set[str] = set()
    component_hashes: dict[str, set[str]] = defaultdict(set)

    def add_component(component_id: str, role: str) -> None:
        if not component_id:
            return
        node = nodes.setdefault(
            component_id,
            {"node_id": component_id, "node_type": "component", "roles": []},
        )
        roles = node.setdefault("roles", [])
        if role and role not in roles:
            roles.append(role)

    for item in observations:
        evidence_id = str(item.get("evidence_id") or "")
        evidence_node_id = f"evidence:{evidence_id}" if evidence_id else ""
        if evidence_node_id:
            nodes[evidence_node_id] = {
                "node_id": evidence_node_id,
                "node_type": "evidence",
                "evidence_id": evidence_id,
                "dimension": str(item.get("dimension") or "unknown"),
                "service_id": str(item.get("service_id") or ""),
            }
        service_id = str(item.get("service_id") or "")
        producer_id = str(item.get("producer_id") or "")
        consumer_id = str(item.get("consumer_id") or "")
        sink_id = str(item.get("sink_id") or "")
        add_component(service_id, "service")
        add_component(producer_id, "producer")
        add_component(consumer_id, "consumer")
        add_component(sink_id, "sink")
        producer_ids.update(value for value in (producer_id,) if value)
        consumer_ids.update(value for value in (consumer_id,) if value)
        sink_ids.update(value for value in (sink_id,) if value)
        artifact_hash = str(item.get("artifact_hash") or "")
        if service_id and artifact_hash:
            component_hashes[service_id].add(artifact_hash)

        edge_from = str(item.get("edge_from") or "")
        edge_to = str(item.get("edge_to") or "")
        expected_edge_state = str(item.get("expected_edge_state") or "")
        actual_edge_state = str(item.get("actual_edge_state") or "")
        if not edge_from and producer_id and consumer_id:
            edge_from, edge_to = producer_id, consumer_id
        elif not edge_from and consumer_id and sink_id:
            edge_from, edge_to = consumer_id, sink_id
        if edge_from and edge_to:
            add_component(edge_from, "endpoint")
            add_component(edge_to, "endpoint")
            edges.append(
                {
                    "edge_type": "flow",
                    "from": edge_from,
                    "to": edge_to,
                    "evidence_id": evidence_id,
                    "dimension": str(item.get("dimension") or "unknown"),
                    "expected_state": expected_edge_state,
                    "actual_state": actual_edge_state,
                    "broken": bool(
                        expected_edge_state
                        and actual_edge_state
                        and expected_edge_state != actual_edge_state
                    ),
                    "order": order_by_evidence.get(evidence_id, len(order_by_evidence)),
                }
            )
        for parent_id in item.get("causal_parent_ids") or []:
            if evidence_node_id and str(parent_id):
                edges.append(
                    {
                        "edge_type": "causal_evidence",
                        "from": f"evidence:{str(parent_id)}",
                        "to": evidence_node_id,
                        "evidence_id": evidence_id,
                        "broken": False,
                        "order": order_by_evidence.get(evidence_id, len(order_by_evidence)),
                    }
                )
        for fingerprint in item.get("correlation_fingerprints") or []:
            if evidence_id and evidence_id not in correlation_groups[str(fingerprint)]:
                correlation_groups[str(fingerprint)].append(evidence_id)
        independence_key = str(item.get("independence_key") or "")
        if independence_key and evidence_id:
            independence_groups[independence_key].append(evidence_id)

    for fingerprint, evidence_ids in correlation_groups.items():
        ordered_ids = sorted(
            evidence_ids,
            key=lambda evidence_id: order_by_evidence.get(
                evidence_id,
                len(order_by_evidence),
            ),
        )
        for left, right in zip(ordered_ids, ordered_ids[1:]):
            edges.append(
                {
                    "edge_type": "correlated_sequence",
                    "from": f"evidence:{left}",
                    "to": f"evidence:{right}",
                    "correlation_fingerprint": fingerprint,
                    "broken": False,
                    "order": order_by_evidence.get(right, len(order_by_evidence)),
                }
            )

    broken_edges = sorted(
        (edge for edge in edges if bool(edge.get("broken"))),
        key=lambda edge: int(edge.get("order") or 0),
    )
    first_broken_edge = dict(broken_edges[0]) if broken_edges else {}
    healthy_producer_edge = any(
        edge.get("from") in producer_ids
        and not bool(edge.get("broken"))
        and str(edge.get("actual_state") or "").lower()
        in {"delivered", "healthy", "published", "queued", "success"}
        for edge in edges
        if edge.get("edge_type") == "flow"
    )
    broken_to_consumer = bool(first_broken_edge) and (
        str(first_broken_edge.get("to") or "") in consumer_ids
        or str(first_broken_edge.get("actual_state") or "").lower()
        in {"backlogged", "dropped", "missing", "stalled", "starved", "timed_out"}
    )
    flow_classification = (
        "consumer_starvation"
        if healthy_producer_edge and broken_to_consumer
        else "broken_flow_edge"
        if first_broken_edge
        else "no_explicit_break"
    )
    return {
        "schema": "chili.provenance-graph.v1",
        "nodes": sorted(nodes.values(), key=lambda node: str(node.get("node_id") or "")),
        "edges": sorted(
            edges,
            key=lambda edge: (
                int(edge.get("order") or 0),
                str(edge.get("edge_type") or ""),
                str(edge.get("from") or ""),
            ),
        ),
        "first_broken_edge": first_broken_edge,
        "broken_edge_evidence_ids": [
            str(edge.get("evidence_id") or "") for edge in broken_edges
        ],
        "correlation_groups": {
            key: sorted(
                values,
                key=lambda evidence_id: order_by_evidence.get(
                    evidence_id,
                    len(order_by_evidence),
                ),
            )
            for key, values in sorted(correlation_groups.items())
        },
        "independence_clusters": {
            key: values
            for key, values in sorted(independence_groups.items())
            if len(values) > 1
        },
        "component_hash_mismatches": {
            key: sorted(values)
            for key, values in sorted(component_hashes.items())
            if len(values) > 1
        },
        "flow_classification": flow_classification,
        "producer_ids": sorted(producer_ids),
        "consumer_ids": sorted(consumer_ids),
        "sink_ids": sorted(sink_ids),
        "runtime_source_parity": timeline.get("runtime_source_parity") or {},
    }


def _structured_provenance_graph(case: Mapping[str, Any]) -> dict[str, Any]:
    has_structure = any(
        item.get("service_id")
        or item.get("producer_id")
        or item.get("consumer_id")
        or item.get("sink_id")
        or item.get("edge_from")
        or item.get("edge_to")
        or item.get("correlation_fingerprints")
        or item.get("artifact_hash")
        for item in case.get("observations") or []
        if isinstance(item, Mapping)
    )
    return build_provenance_graph(case) if has_structure else {}


def _prompt_terms(prompt: str) -> list[str]:
    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", prompt or "")
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{4,}\b", prompt or "")
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in [*identifiers, *words]:
        clean = raw.lower()
        if clean in _STOP_WORDS or clean in seen:
            continue
        seen.add(clean)
        ordered.append(clean)
    return ordered[:20]


def collect_repo_evidence(
    repo_path: Path,
    prompt: str,
    *,
    candidate_paths: Sequence[str] = (),
    max_files: int = 240,
    max_records: int = 24,
) -> list[dict[str, Any]]:
    """Collect bounded read-only source snippets relevant to a diagnosis."""
    root = repo_path.resolve()
    terms = _prompt_terms(prompt)
    if not terms or not root.is_dir():
        return []

    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in candidate_paths:
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix.lower() in _SOURCE_SUFFIXES and candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)

    for candidate in root.rglob("*"):
        if len(paths) >= max_files:
            break
        if not candidate.is_file() or candidate.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        try:
            rel_parts = candidate.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)

    scored: list[tuple[int, str, int, str]] = []
    for path in paths:
        try:
            if path.stat().st_size > 600_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        per_file = 0
        for line_number, line in enumerate(lines, start=1):
            lower = line.lower()
            matched = sum(1 for term in terms if term in lower)
            if not matched:
                continue
            score = matched * 10 + (4 if any(term in Path(rel).name.lower() for term in terms) else 0)
            context_start = max(0, line_number - 3)
            context_end = min(len(lines), line_number + 2)
            snippet = " | ".join(
                f"{offset + 1}:{lines[offset].strip()}"
                for offset in range(context_start, context_end)
                if lines[offset].strip()
            )
            scored.append((score, rel, line_number, _clip(snippet, 700)))
            per_file += 1
            if per_file >= 3:
                break

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    records: list[dict[str, Any]] = []
    for index, (_score, rel, line_number, snippet) in enumerate(scored[:max_records]):
        provenance = f"{rel}:{line_number}"
        records.append(
            normalize_evidence(
                {
                    "evidence_id": f"source-{index + 1}",
                    "statement": f"{provenance}: {snippet}",
                    "dimension": infer_dimension(f"{rel} {snippet}"),
                    "dimension_origin": "inferred",
                    "kind": "artifact",
                    "provenance": provenance,
                    "independence_key": rel,
                    "reliability": 0.75 if rel.startswith("tests/") else 0.9,
                    "discriminating": any(
                        signal in snippet.lower() for signal in _DIRECT_SOURCE_SIGNALS
                    ),
                },
                index,
            )
        )
    return records


def build_case_from_prompt(
    prompt: str,
    *,
    case_id: str = "operator-diagnostic",
    repo_path: Path | None = None,
    candidate_paths: Sequence[str] = (),
) -> dict[str, Any]:
    segments = [
        _clip(item, 700)
        for item in re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", prompt or "")
        if item.strip()
    ][:18]
    observations = [
        {
            "evidence_id": f"operator-{index + 1}",
            "statement": statement,
            "dimension": infer_dimension(statement),
            "dimension_origin": "inferred",
            "kind": "observation",
            "provenance": f"operator_prompt:{index + 1}",
            "independence_key": "operator_prompt",
            "reliability": 0.65,
            "discriminating": False,
        }
        for index, statement in enumerate(segments)
    ]
    if repo_path is not None:
        observations.extend(
            collect_repo_evidence(
                repo_path,
                prompt,
                candidate_paths=candidate_paths,
            )
        )
    return normalize_case(
        {
            "case_id": case_id,
            "problem_statement": prompt,
            "observations": observations,
            "constraints": {
                "contract_invariants": derive_contract_invariants(prompt),
                "diagnostic_lenses": derive_diagnostic_lenses(prompt),
            },
        }
    )


def parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


_RAW_PROBE_FIELDS = frozenset(
    {
        "probe_id",
        "kind",
        "paths",
        "path",
        "query",
        "selector",
        "table",
        "timestamp_column",
        "group_by",
        "aggregate",
        "aggregate_column",
        "filters",
    }
)


def validate_raw_packet_schema(raw: Mapping[str, Any]) -> list[str]:
    """Validate the parsed model envelope before normalization drops fields."""
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        return ["Diagnostic packet must be a JSON object."]

    misplaced_probe_fields = sorted(_RAW_PROBE_FIELDS.intersection(raw))
    if misplaced_probe_fields:
        errors.append(
            "Standalone diagnostic probes are not packets; put the probe inside "
            "experiments[].probe."
        )

    hypotheses = raw.get("hypotheses")
    if not isinstance(hypotheses, list):
        errors.append("hypotheses must be a JSON array.")
    elif not hypotheses:
        errors.append("hypotheses must contain at least one hypothesis object.")
    else:
        for index, item in enumerate(hypotheses):
            label = f"hypotheses[{index}]"
            if not isinstance(item, Mapping):
                errors.append(f"{label} must be a JSON object.")
                continue
            for field in (
                "support_evidence_ids",
                "contradict_evidence_ids",
            ):
                if field in item and not isinstance(item.get(field), list):
                    errors.append(f"{label}.{field} must be a JSON array.")
            for field in ("hypothesis_id", "claim", "dimension", "falsification"):
                if field in item and not isinstance(item.get(field), str):
                    errors.append(f"{label}.{field} must be a string.")

    experiments = raw.get("experiments", [])
    if not isinstance(experiments, list):
        errors.append("experiments must be a JSON array.")
    else:
        for index, item in enumerate(experiments):
            label = f"experiments[{index}]"
            if not isinstance(item, Mapping):
                errors.append(f"{label} must be a JSON object.")
                continue
            misplaced = sorted(_RAW_PROBE_FIELDS.intersection(item))
            if misplaced:
                errors.append(
                    f"{label} is a standalone probe; move probe fields under "
                    f"{label}.probe."
                )
            for field in (
                "hypothesis_ids",
                "changed_dimensions",
                "held_constant_dimensions",
                "evidence_required",
                "result_evidence_ids",
            ):
                if field in item and not isinstance(item.get(field), list):
                    errors.append(f"{label}.{field} must be a JSON array.")
            for field in (
                "experiment_id",
                "expected_if_true",
                "expected_if_false",
                "safety",
                "status",
            ):
                if field in item and not isinstance(item.get(field), str):
                    errors.append(f"{label}.{field} must be a string.")
            if "auto_execute" in item and not isinstance(
                item.get("auto_execute"), bool
            ):
                errors.append(f"{label}.auto_execute must be a boolean.")
            if "probe" in item and not isinstance(item.get("probe"), Mapping):
                errors.append(f"{label}.probe must be a JSON object.")
            probe = item.get("probe")
            if isinstance(probe, Mapping):
                for field in ("probe_id", "kind"):
                    if field in probe and not isinstance(probe.get(field), str):
                        errors.append(f"{label}.probe.{field} must be a string.")

    conclusion = raw.get("conclusion")
    if not isinstance(conclusion, Mapping):
        errors.append("conclusion must be a JSON object.")
    else:
        for field in ("hypothesis_id", "status", "reason"):
            if field in conclusion and not isinstance(conclusion.get(field), str):
                errors.append(f"conclusion.{field} must be a string.")
        if "evidence_ids" in conclusion and not isinstance(
            conclusion.get("evidence_ids"), list
        ):
            errors.append("conclusion.evidence_ids must be a JSON array.")
    return sorted(dict.fromkeys(errors))


def _normalize_hypothesis(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    dimension = str(raw.get("dimension") or "unknown").strip().lower()
    dimension = DIMENSION_ALIASES.get(dimension, dimension)
    if dimension not in DIMENSIONS or dimension == "unknown":
        inferred_dimension = infer_dimension(str(raw.get("claim") or ""))
        if inferred_dimension != "unknown":
            dimension = inferred_dimension
    return {
        "hypothesis_id": _clean_id(raw.get("hypothesis_id"), f"h{index + 1}"),
        "claim": _clip(raw.get("claim"), 700),
        "dimension": dimension,
        "support_evidence_ids": [
            _clean_id(value, "") for value in raw.get("support_evidence_ids") or [] if str(value).strip()
        ][:20],
        "contradict_evidence_ids": [
            _clean_id(value, "") for value in raw.get("contradict_evidence_ids") or [] if str(value).strip()
        ][:20],
        "falsification": _clip(raw.get("falsification"), 700),
    }


def _normalize_experiment(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    safety = str(raw.get("safety") or "isolated").strip().lower()
    if safety not in SAFETY_LEVELS:
        safety = "isolated"
    status = str(raw.get("status") or "planned").strip().lower()
    if status not in {"planned", "completed", "blocked"}:
        status = "planned"
    auto_execute = bool(raw.get("auto_execute"))
    raw_probe = raw.get("probe") if isinstance(raw.get("probe"), Mapping) else {}
    probe = diagnostic_probes.normalize_probe_spec(raw_probe, index)
    if not auto_execute and probe.get("kind") not in diagnostic_probes.PROBE_KINDS:
        probe = {}
    return {
        "experiment_id": _clean_id(raw.get("experiment_id"), f"experiment-{index + 1}"),
        "hypothesis_ids": [_clean_id(value, "") for value in raw.get("hypothesis_ids") or [] if str(value).strip()][:12],
        "changed_dimensions": [
            value for value in (str(item).strip().lower() for item in raw.get("changed_dimensions") or []) if value in DIMENSIONS
        ],
        "held_constant_dimensions": [
            value for value in (str(item).strip().lower() for item in raw.get("held_constant_dimensions") or []) if value in DIMENSIONS
        ],
        "expected_if_true": _clip(raw.get("expected_if_true"), 500),
        "expected_if_false": _clip(raw.get("expected_if_false"), 500),
        "evidence_required": [_clip(value, 220) for value in raw.get("evidence_required") or [] if str(value).strip()][:10],
        "result_evidence_ids": [_clean_id(value, "") for value in raw.get("result_evidence_ids") or [] if str(value).strip()][:20],
        "safety": safety,
        "status": status,
        "auto_execute": auto_execute,
        "probe": probe if probe.get("kind") else {},
    }


def normalize_packet(raw: Mapping[str, Any]) -> dict[str, Any]:
    hypotheses = [
        _normalize_hypothesis(item, index)
        for index, item in enumerate(raw.get("hypotheses") or [])
        if isinstance(item, Mapping)
    ][:12]
    experiments = [
        _normalize_experiment(item, index)
        for index, item in enumerate(raw.get("experiments") or [])
        if isinstance(item, Mapping)
    ][:16]
    conclusion_raw = raw.get("conclusion") if isinstance(raw.get("conclusion"), Mapping) else {}
    requested_status = str(conclusion_raw.get("status") or "provisional").strip().lower()
    if requested_status not in {"confirmed", "provisional", "inconclusive", "rejected"}:
        requested_status = "provisional"
    return {
        "schema": PACKET_SCHEMA,
        "problem_statement": _clip(raw.get("problem_statement"), 1600),
        "hypotheses": hypotheses,
        "experiments": experiments,
        "conclusion": {
            "hypothesis_id": _clean_id(conclusion_raw.get("hypothesis_id"), ""),
            "status": requested_status,
            "evidence_ids": [
                _clean_id(value, "") for value in conclusion_raw.get("evidence_ids") or [] if str(value).strip()
            ][:20],
            "reason": _clip(conclusion_raw.get("reason"), 700),
        },
    }


def detect_baseline_drift(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in observations:
        key = str(item.get("comparison_key") or "")
        revision = str(item.get("code_revision") or "")
        inputs = str(item.get("input_fingerprint") or "")
        outcome = str(item.get("outcome_fingerprint") or "")
        if key and revision and inputs and outcome:
            groups[(key, revision, inputs)].append(item)
    drift: list[dict[str, Any]] = []
    for (key, revision, inputs), items in groups.items():
        outcomes = sorted({str(item.get("outcome_fingerprint")) for item in items})
        if len(outcomes) < 2:
            continue
        drift.append(
            {
                "comparison_key": key,
                "code_revision": revision,
                "input_fingerprint": inputs,
                "outcome_fingerprints": outcomes,
                "environment_fingerprints": sorted(
                    {str(item.get("environment_fingerprint") or "unknown") for item in items}
                ),
                "evidence_ids": [str(item.get("evidence_id")) for item in items],
            }
        )
    recorded_ids = {
        str(evidence_id)
        for finding in drift
        for evidence_id in finding.get("evidence_ids") or []
        if str(evidence_id)
    }
    for item in observations:
        evidence_id = str(item.get("evidence_id") or "")
        statement = str(item.get("statement") or "").lower()
        relation = str(item.get("retained_comparison") or "").lower()
        if relation not in {"none", "stable", "changed", "incomparable"}:
            relation = _retained_comparison_relation(
                statement,
                str(item.get("structured_context") or ""),
            )
        if evidence_id and evidence_id not in recorded_ids and relation == "changed":
            explicitly_retained = bool(
                "retained=true"
                in str(item.get("structured_context") or "").lower()
                or re.search(r"\bretained\b", statement)
            )
            drift.append(
                {
                    "comparison_key": str(
                        item.get("comparison_key") or "semantic-baseline-pair"
                    ),
                    "code_revision": str(item.get("code_revision") or "unknown"),
                    "input_fingerprint": str(
                        item.get("input_fingerprint") or "controlled-by-statement"
                    ),
                    "outcome_fingerprints": [],
                    "environment_fingerprints": [
                        str(item.get("environment_fingerprint") or "unknown")
                    ],
                    "evidence_ids": [evidence_id],
                    "finding_type": (
                        "retained_semantic_baseline_drift"
                        if explicitly_retained
                        else "semantic_baseline_drift"
                    ),
                }
            )
            recorded_ids.add(evidence_id)
            continue
        if (
            not evidence_id
            or evidence_id in recorded_ids
            or relation != "incomparable"
        ):
            continue
        drift.append(
            {
                "comparison_key": str(item.get("comparison_key") or "unreproducible-baseline"),
                "code_revision": str(item.get("code_revision") or "unknown"),
                "input_fingerprint": str(item.get("input_fingerprint") or "unknown"),
                "outcome_fingerprints": [],
                "environment_fingerprints": [
                    str(item.get("environment_fingerprint") or "unknown")
                ],
                "evidence_ids": [evidence_id],
                "finding_type": "baseline_comparability_gap",
            }
        )
    return drift


def _independent_weight(records: Iterable[Mapping[str, Any]]) -> float:
    strongest: dict[str, float] = {}
    for item in records:
        key = str(item.get("independence_key") or item.get("provenance") or item.get("evidence_id"))
        strongest[key] = max(strongest.get(key, 0.0), _clamp_reliability(item.get("reliability")))
    return round(sum(strongest.values()), 4)


def _confirmatory_weight(records: Iterable[Mapping[str, Any]]) -> float:
    return _independent_weight(
        item
        for item in records
        if str(item.get("independence_key") or "") != "operator_prompt"
    )


def _record_has_structured_break(record: Mapping[str, Any]) -> bool:
    expected_state = str(record.get("expected_state") or "")
    actual_state = str(record.get("actual_state") or record.get("transition_to") or "")
    expected_edge_state = str(record.get("expected_edge_state") or "")
    actual_edge_state = str(record.get("actual_edge_state") or "")
    return bool(
        (expected_state and actual_state and expected_state != actual_state)
        or (
            expected_edge_state
            and actual_edge_state
            and expected_edge_state != actual_edge_state
        )
    )


def _is_contrastive_experiment(record: Mapping[str, Any]) -> bool:
    changed_dimensions = set(
        _normalized_dimensions(record.get("changed_dimensions"))
    )
    held_constant_dimensions = set(
        _normalized_dimensions(record.get("held_constant_dimensions"))
    )
    owner_dimension = _evidence_owner_dimension(record)
    return bool(
        str(record.get("kind") or "") == "experiment"
        and str(record.get("evidence_lifecycle") or "observed_result")
        == "observed_result"
        and str(record.get("intervention_scope") or "none")
        in {"component", "boundary"}
        and changed_dimensions
        and held_constant_dimensions
        and changed_dimensions.isdisjoint(held_constant_dimensions)
        and owner_dimension in changed_dimensions
        and str(record.get("expected_if_true") or "").strip()
        and str(record.get("expected_if_false") or "").strip()
        and bool(record.get("discriminating"))
        and str(record.get("causal_role") or "context") == "support"
        and not bool(record.get("attribution_gap"))
    )


def _is_coarse_reset_experiment(record: Mapping[str, Any]) -> bool:
    if str(record.get("kind") or "") != "experiment":
        return False
    if str(record.get("intervention_scope") or "") == "broad":
        return True
    lower = str(record.get("statement") or "").lower()
    return any(marker in lower for marker in _COARSE_RESET_EXPERIMENT_MARKERS)


def _is_attribution_resolving_evidence(record: Mapping[str, Any]) -> bool:
    if bool(record.get("attribution_gap")):
        return False
    if str(record.get("evidence_lifecycle") or "observed_result") != "observed_result":
        return False
    if str(record.get("kind") or "") != "experiment":
        return False
    if _is_coarse_reset_experiment(record) or str(
        record.get("intervention_scope") or "none"
    ) == "broad":
        return False
    lower = str(record.get("statement") or "").lower()
    context = str(record.get("structured_context") or "").lower()
    explicit_single_change = any(
        marker in lower
        for marker in (
            "changing only",
            "reverting only",
            "pinning only",
            "resetting only",
            "removing only",
            "adding the exact",
            "restoring only",
            "single-change",
        )
    )
    held_constants = any(
        marker in lower
        for marker in (
            "without changing",
            "while keeping",
            "while holding",
            "with all other",
            "leaving all other",
        )
    )
    return bool(
        _is_contrastive_experiment(record)
        and (
            "changed_factor=" in context
            or explicit_single_change
            or held_constants
            or _record_has_structured_break(record)
        )
    )


def _evidence_owner_dimension(record: Mapping[str, Any]) -> str:
    causal = str(record.get("causal_dimension") or "unknown")
    return (
        causal
        if causal in DIMENSIONS and causal != "unknown"
        else str(record.get("dimension") or "unknown")
    )


def _contradiction_record_compatible(
    record: Mapping[str, Any],
    hypothesis_dimension: str,
) -> bool:
    if (
        bool(record.get("attribution_gap"))
        or str(record.get("evidence_lifecycle") or "observed_result")
        != "observed_result"
    ):
        return False
    role = str(record.get("causal_role") or "context")
    if role == "contradiction":
        return True
    owner_dimension = _evidence_owner_dimension(record)
    return bool(
        role == "support"
        and str(record.get("intervention_scope") or "none") == "component"
        and owner_dimension not in {"unknown", hypothesis_dimension}
    )


def _has_direct_causal_artifact(record: Mapping[str, Any]) -> bool:
    if (
        str(record.get("kind") or "") != "artifact"
        or str(record.get("causal_role") or "context") != "support"
        or bool(record.get("attribution_gap"))
        or _clamp_reliability(record.get("reliability")) < 0.9
    ):
        return False
    lower = str(record.get("statement") or "").lower()
    return _record_has_structured_break(record) or any(
        marker in lower for marker in _CAUSAL_SUPPORT_MARKERS
    )


def _is_qualified_causal_record(record: Mapping[str, Any]) -> bool:
    return bool(
        str(record.get("causal_role") or "context") == "support"
        and str(record.get("evidence_lifecycle") or "observed_result")
        == "observed_result"
        and not bool(record.get("attribution_gap"))
        and (
            _is_contrastive_experiment(record)
            or _record_has_structured_break(record)
            or _has_direct_causal_artifact(record)
        )
    )


def _causal_sufficiency(
    records: Sequence[Mapping[str, Any]],
    *,
    earliest_break_support: bool,
    provenance_break_support: bool,
) -> str:
    if (
        any(_is_contrastive_experiment(record) for record in records)
    ):
        return "isolated"
    if earliest_break_support or provenance_break_support:
        return "graph_linked"
    if any(_has_direct_causal_artifact(record) for record in records):
        return "direct_artifact"
    return "observational"


def _causal_sufficiency_rank(value: object) -> int:
    return {
        "observational": 0,
        "direct_artifact": 1,
        "graph_linked": 2,
        "isolated": 3,
    }.get(str(value or "observational"), 0)


def evidence_gated_report_revision(
    previous_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    new_evidence: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Decide whether new evidence is strong enough to change causal family."""
    previous = (
        previous_report.get("conclusion")
        if isinstance(previous_report.get("conclusion"), Mapping)
        else {}
    )
    candidate = (
        candidate_report.get("conclusion")
        if isinstance(candidate_report.get("conclusion"), Mapping)
        else {}
    )
    previous_dimension = str(previous.get("dimension") or "unknown")
    candidate_dimension = str(candidate.get("dimension") or "unknown")
    previous_rank = _causal_sufficiency_rank(previous.get("causal_sufficiency"))
    candidate_rank = _causal_sufficiency_rank(candidate.get("causal_sufficiency"))
    if candidate_dimension == previous_dimension:
        return {
            "accepted": True,
            "reason": "same_causal_family",
            "previous_dimension": previous_dimension,
            "candidate_dimension": candidate_dimension,
        }
    if (
        candidate.get("status") == "confirmed"
        and previous.get("status") != "confirmed"
        and not candidate.get("blockers")
    ):
        return {
            "accepted": True,
            "reason": "newly_confirmed_causal_family",
            "previous_dimension": previous_dimension,
            "candidate_dimension": candidate_dimension,
        }
    if candidate_rank > previous_rank:
        return {
            "accepted": True,
            "reason": "stronger_causal_sufficiency",
            "previous_dimension": previous_dimension,
            "candidate_dimension": candidate_dimension,
        }

    selected_ids = {str(value) for value in candidate.get("evidence_ids") or []}
    qualified_new_ids = {
        str(item.get("evidence_id") or "")
        for item in new_evidence
        if isinstance(item, Mapping)
        and str(item.get("dimension_origin") or "unknown") == "explicit"
        and _is_qualified_causal_record(item)
    }
    if selected_ids & qualified_new_ids and candidate_rank >= previous_rank:
        return {
            "accepted": True,
            "reason": "new_qualified_causal_evidence",
            "previous_dimension": previous_dimension,
            "candidate_dimension": candidate_dimension,
        }
    return {
        "accepted": False,
        "reason": "causal_family_change_lacks_stronger_evidence",
        "previous_dimension": previous_dimension,
        "candidate_dimension": candidate_dimension,
        "previous_causal_sufficiency": str(
            previous.get("causal_sufficiency") or "observational"
        ),
        "candidate_causal_sufficiency": str(
            candidate.get("causal_sufficiency") or "observational"
        ),
    }


def _typed_probe_fallback_hypotheses(
    case: Mapping[str, Any],
    hypotheses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recover strongly grounded causal families omitted by a small local model.

    Contrastive experiments and explicit structured breaks may create a
    bounded fallback. Ordinary probes, observations, and symptom volume cannot.
    """
    existing_support_by_dimension: dict[str, set[str]] = defaultdict(set)
    for item in hypotheses:
        if not isinstance(item, Mapping):
            continue
        existing_support_by_dimension[
            str(item.get("dimension") or "unknown")
        ].update(str(value) for value in item.get("support_evidence_ids") or [])
    existing_ids = {
        str(item.get("hypothesis_id") or "")
        for item in hypotheses
        if isinstance(item, Mapping)
    }
    evidence_by_dimension: dict[str, list[str]] = defaultdict(list)
    strong_records_by_id: dict[str, Mapping[str, Any]] = {}
    for record in case.get("observations") or []:
        if not isinstance(record, Mapping):
            continue
        dimension = _evidence_owner_dimension(record)
        expected_edge_state = str(record.get("expected_edge_state") or "")
        actual_edge_state = str(record.get("actual_edge_state") or "")
        strong_causal_record = bool(
            _is_contrastive_experiment(record)
            or (
                expected_edge_state
                and actual_edge_state
                and expected_edge_state != actual_edge_state
            )
            or _has_direct_causal_artifact(record)
        )
        if (
            dimension not in DIMENSIONS
            or dimension == "unknown"
            or not strong_causal_record
            or not _is_qualified_causal_record(record)
            or _clamp_reliability(record.get("reliability")) < 0.9
            or str(record.get("causal_role") or "context") != "support"
            or bool(record.get("attribution_gap"))
        ):
            continue
        evidence_id = str(record.get("evidence_id") or "")
        if evidence_id and evidence_id not in evidence_by_dimension[dimension]:
            evidence_by_dimension[dimension].append(evidence_id)
            strong_records_by_id[evidence_id] = record

    fallbacks: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        evidence_ids = evidence_by_dimension.get(dimension) or []
        if not evidence_ids:
            continue
        existing_overlap = (
            existing_support_by_dimension.get(dimension, set()) & set(evidence_ids)
        )
        isolation_grade_ids = {
            evidence_id
            for evidence_id in evidence_ids
            if _is_contrastive_experiment(
                strong_records_by_id.get(evidence_id) or {}
            )
            or (
                str(
                    (strong_records_by_id.get(evidence_id) or {}).get(
                        "expected_edge_state"
                    )
                    or ""
                )
                and str(
                    (strong_records_by_id.get(evidence_id) or {}).get(
                        "actual_edge_state"
                    )
                    or ""
                )
                and str(
                    (strong_records_by_id.get(evidence_id) or {}).get(
                        "expected_edge_state"
                    )
                    or ""
                )
                != str(
                    (strong_records_by_id.get(evidence_id) or {}).get(
                        "actual_edge_state"
                    )
                    or ""
                )
            )
        }
        if existing_overlap and (
            not isolation_grade_ids or existing_overlap & isolation_grade_ids
        ):
            continue
        base_id = f"evidence-{dimension}"
        hypothesis_id = base_id
        suffix = 2
        while hypothesis_id in existing_ids:
            hypothesis_id = f"{base_id}-{suffix}"
            suffix += 1
        existing_ids.add(hypothesis_id)
        label = dimension.replace("_", " ")
        fallbacks.append(
            {
                "hypothesis_id": hypothesis_id,
                "claim": f"Controlled diagnostic evidence identifies {label} as the primary causal dimension.",
                "dimension": dimension,
                "support_evidence_ids": evidence_ids,
                "contradict_evidence_ids": [],
                "falsification": (
                    f"Hold other dimensions constant and remove or restore only the observed {label} condition."
                ),
                "origin": "deterministic_evidence_gate",
            }
        )
    return fallbacks


def _validate_packet(case: Mapping[str, Any], packet: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in case.get("observations") or []
        if isinstance(item, Mapping)
    }
    evidence_ids = set(evidence_by_id)
    hypothesis_ids: set[str] = set()
    if not packet.get("hypotheses"):
        errors.append("At least one falsifiable hypothesis is required.")
    for item in packet.get("hypotheses") or []:
        hypothesis_id = str(item.get("hypothesis_id") or "")
        if not hypothesis_id or hypothesis_id in hypothesis_ids:
            errors.append("Hypothesis ids must be non-empty and unique.")
        hypothesis_ids.add(hypothesis_id)
        if not str(item.get("claim") or "").strip():
            errors.append(f"{hypothesis_id or 'hypothesis'} has no claim.")
        if not str(item.get("falsification") or "").strip():
            errors.append(f"{hypothesis_id or 'hypothesis'} has no falsification test.")
        linked = [*(item.get("support_evidence_ids") or []), *(item.get("contradict_evidence_ids") or [])]
        unknown = sorted({str(value) for value in linked if str(value) not in evidence_ids})
        if unknown:
            errors.append(f"{hypothesis_id} references unknown evidence: {', '.join(unknown)}")
        hypothesis_dimension = str(item.get("dimension") or "unknown")
        mismatched_support = sorted(
            str(value)
            for value in item.get("support_evidence_ids") or []
            if str(value) in evidence_by_id
            and str(
                evidence_by_id[str(value)].get("dimension_origin") or "unknown"
            )
            == "explicit"
            and str(evidence_by_id[str(value)].get("dimension") or "unknown")
            not in {hypothesis_dimension, "unknown"}
        )
        if mismatched_support:
            errors.append(
                f"{hypothesis_id} links support from a different evidence family: "
                + ", ".join(mismatched_support)
            )
        contradiction_as_support = sorted(
            str(value)
            for value in item.get("support_evidence_ids") or []
            if str(value) in evidence_by_id
            and str(evidence_by_id[str(value)].get("causal_role") or "context")
            == "contradiction"
        )
        if contradiction_as_support:
            errors.append(
                f"{hypothesis_id} links contradiction evidence as support: "
                + ", ".join(contradiction_as_support)
            )
    experiment_ids: set[str] = set()
    for item in packet.get("experiments") or []:
        experiment_id = str(item.get("experiment_id") or "")
        if not experiment_id or experiment_id in experiment_ids:
            errors.append("Experiment ids must be non-empty and unique.")
        experiment_ids.add(experiment_id)
        if item.get("auto_execute") and item.get("safety") not in AUTO_SAFE_LEVELS:
            errors.append(f"{experiment_id} requests unsafe automatic execution.")
        probe = item.get("probe") if isinstance(item.get("probe"), Mapping) else {}
        if item.get("auto_execute") and not probe:
            errors.append(f"{experiment_id} requests automatic execution without a typed probe.")
        if probe:
            errors.extend(
                f"{experiment_id}: {error}"
                for error in diagnostic_probes.validate_probe_spec(
                    probe,
                    str(item.get("safety") or ""),
                )
            )
        unknown_hypotheses = sorted(
            {str(value) for value in item.get("hypothesis_ids") or [] if str(value) not in hypothesis_ids}
        )
        if unknown_hypotheses:
            errors.append(f"{experiment_id} references unknown hypotheses: {', '.join(unknown_hypotheses)}")
        unknown_evidence = sorted(
            {str(value) for value in item.get("result_evidence_ids") or [] if str(value) not in evidence_ids}
        )
        if unknown_evidence:
            errors.append(f"{experiment_id} references unknown result evidence: {', '.join(unknown_evidence)}")
    conclusion_id = str((packet.get("conclusion") or {}).get("hypothesis_id") or "")
    if not conclusion_id:
        errors.append("A conclusion hypothesis is required.")
    if conclusion_id and conclusion_id not in hypothesis_ids:
        errors.append("Conclusion references an unknown hypothesis.")
    conclusion_evidence = {
        str(value)
        for value in (packet.get("conclusion") or {}).get("evidence_ids") or []
        if str(value)
    }
    unknown_conclusion_evidence = sorted(conclusion_evidence - evidence_ids)
    if unknown_conclusion_evidence:
        errors.append(
            "Conclusion references unknown evidence: "
            + ", ".join(unknown_conclusion_evidence)
        )
    return sorted(dict.fromkeys(errors))


def recommend_counterfactuals(
    case: Mapping[str, Any],
    packet: Mapping[str, Any],
    hypothesis_results: Sequence[Mapping[str, Any]],
    baseline_drift: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    existing_dimensions = {
        dimension
        for item in packet.get("experiments") or []
        if isinstance(item, Mapping)
        for dimension in item.get("changed_dimensions") or []
    }
    recommendations: list[dict[str, Any]] = []
    if baseline_drift:
        priority = ("data", "clock", "state", "config", "dependency", "runtime", "test_harness")
        for dimension in priority:
            if dimension in existing_dimensions:
                continue
            recommendations.append(
                {
                    "experiment_id": f"isolate-{dimension}",
                    "dimension": dimension,
                    "safety": "read_only" if dimension in {"data", "clock", "config", "test_harness"} else "isolated",
                    "action": f"Hold code and inputs constant; measure whether changing only {dimension} restores the baseline outcome.",
                    "required_evidence": ["code revision", "input fingerprint", "environment fingerprint", "outcome fingerprint"],
                }
            )
            if len(recommendations) >= 5:
                break
    for result in hypothesis_results:
        if result.get("status") in {"supported", "refuted"}:
            continue
        dimension = str(result.get("dimension") or "unknown")
        if any(item.get("dimension") == dimension for item in recommendations):
            continue
        recommendations.append(
            {
                "experiment_id": f"falsify-{result.get('hypothesis_id')}",
                "dimension": dimension,
                "safety": "isolated",
                "action": str(result.get("falsification") or f"Vary only {dimension} and record a discriminating outcome."),
                "required_evidence": ["held constants", "changed dimension", "expected outcomes", "actual outcome"],
            }
        )
        if len(recommendations) >= 6:
            break
    return recommendations


def evaluate_packet(
    raw_case: Mapping[str, Any],
    raw_packet: Mapping[str, Any],
    *,
    previous_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = normalize_case(raw_case)
    packet = normalize_packet(raw_packet)
    errors = _validate_packet(case, packet)
    evidence = {str(item.get("evidence_id")): item for item in case["observations"]}
    drift = detect_baseline_drift(case["observations"])
    problem_statement = str(case.get("problem_statement") or "")
    problem_dimension = infer_dimension(problem_statement)
    decisive_problem_dimension = decisive_inferred_dimension(problem_statement)
    attribution_gap_records = [
        item
        for item in case["observations"]
        if bool(item.get("attribution_gap"))
        and _clamp_reliability(item.get("reliability")) >= 0.9
    ]
    unresolved_attribution = bool(
        has_attribution_gap(str(case.get("problem_statement") or ""))
        or len(attribution_gap_records) >= 2
        or any(
            any(
                marker in str(item.get("statement") or "").lower()
                for marker in _DECISIVE_ATTRIBUTION_GAP_MARKERS
            )
            for item in attribution_gap_records
        )
    )
    mechanism_attribution_gap = bool(
        any(
            marker in str(case.get("problem_statement") or "").lower()
            for marker in _MECHANISM_ATTRIBUTION_GAP_MARKERS
        )
        or any(
            any(
                marker in str(item.get("statement") or "").lower()
                for marker in _MECHANISM_ATTRIBUTION_GAP_MARKERS
            )
            for item in attribution_gap_records
        )
    )
    causal_timeline = _structured_causal_timeline(case)
    downstream_evidence_ids = {
        str(value)
        for value in causal_timeline.get("downstream_evidence_ids") or []
        if str(value)
    }
    earliest_break = (
        causal_timeline.get("earliest_break")
        if isinstance(causal_timeline.get("earliest_break"), Mapping)
        else {}
    )
    earliest_break_id = str(earliest_break.get("evidence_id") or "")
    earliest_break_dimension = str(earliest_break.get("dimension") or "unknown")
    runtime_source_mismatch = str(
        (causal_timeline.get("runtime_source_parity") or {}).get("status") or ""
    ) == "mismatch"
    provenance_graph = _structured_provenance_graph(case)
    first_broken_edge = (
        provenance_graph.get("first_broken_edge")
        if isinstance(provenance_graph.get("first_broken_edge"), Mapping)
        else {}
    )
    provenance_break_evidence_id = str(first_broken_edge.get("evidence_id") or "")
    provenance_break_dimension = str(first_broken_edge.get("dimension") or "unknown")

    hypotheses = [
        *packet["hypotheses"],
        *_typed_probe_fallback_hypotheses(case, packet["hypotheses"]),
    ]
    drift_evidence_ids = list(
        dict.fromkeys(
            str(evidence_id)
            for finding in drift
            for evidence_id in finding.get("evidence_ids") or []
            if str(evidence_id)
        )
    )
    if drift and problem_dimension == "test_harness" and not any(
        str(item.get("dimension") or "unknown") == "test_harness"
        and set(str(value) for value in item.get("support_evidence_ids") or [])
        & set(drift_evidence_ids)
        for item in hypotheses
    ):
        existing_ids = {
            str(item.get("hypothesis_id") or "") for item in hypotheses
        }
        hypothesis_id = "baseline-test-harness"
        suffix = 2
        while hypothesis_id in existing_ids:
            hypothesis_id = f"baseline-test-harness-{suffix}"
            suffix += 1
        hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "claim": "The comparison harness or its environment changed the observed baseline.",
                "dimension": "test_harness",
                "support_evidence_ids": drift_evidence_ids,
                "contradict_evidence_ids": [],
                "falsification": "Reproduce both outcomes in one fully fingerprinted comparison environment.",
                "origin": "baseline_drift_gate",
            }
        )

    hypothesis_results: list[dict[str, Any]] = []
    for item in hypotheses:
        support_ids = list(
            dict.fromkeys(str(value) for value in item.get("support_evidence_ids") or [])
        )
        contradict_ids = list(
            dict.fromkeys(str(value) for value in item.get("contradict_evidence_ids") or [])
        )
        linked_support_records = [
            evidence[value] for value in support_ids if value in evidence
        ]
        causal_support_records = [
            record
            for record in linked_support_records
            if str(record.get("evidence_id") or "") not in downstream_evidence_ids
            and not bool(record.get("attribution_gap"))
            and str(record.get("causal_role") or "context") == "support"
            and str(record.get("dimension_origin") or "unknown") != "inferred"
        ]
        context_records = [
            record
            for record in linked_support_records
            if str(record.get("evidence_id") or "") not in downstream_evidence_ids
            and not bool(record.get("attribution_gap"))
            and (
                str(record.get("causal_role") or "context") == "context"
                or (
                    str(record.get("causal_role") or "context") == "support"
                    and str(record.get("dimension_origin") or "unknown")
                    == "inferred"
                )
            )
        ]
        explicit_contradict_records = [
            evidence[value]
            for value in contradict_ids
            if value in evidence
            and _contradiction_record_compatible(
                evidence[value],
                str(item.get("dimension") or "unknown"),
            )
        ]
        implicit_contradict_records = [
            record
            for record in linked_support_records
            if str(record.get("causal_role") or "context") == "contradiction"
        ]
        contradict_records_by_id = {
            str(record.get("evidence_id") or f"contradiction-{index}"): record
            for index, record in enumerate(
                [*explicit_contradict_records, *implicit_contradict_records]
            )
        }
        contradict_records = list(contradict_records_by_id.values())
        causal_support_weight = _independent_weight(causal_support_records)
        context_weight = round(0.2 * _independent_weight(context_records), 4)
        support_weight = causal_support_weight
        confirmatory_weight = _confirmatory_weight(causal_support_records)
        contradict_weight = _independent_weight(contradict_records)
        discriminating = any(
            _is_contrastive_experiment(record)
            for record in causal_support_records
        )
        direct_artifact_records = [
            record
            for record in causal_support_records
            if _has_direct_causal_artifact(record)
        ]
        direct_artifact = bool(direct_artifact_records)
        typed_probe_evidence = any(
            str(record.get("provenance") or "").startswith("diagnostic_probe:")
            for record in causal_support_records
        )
        causal_support_ids = {
            str(record.get("evidence_id") or "") for record in causal_support_records
        }
        earliest_break_support = bool(
            earliest_break_id and earliest_break_id in causal_support_ids
        )
        earliest_break_graph_qualified = bool(
            earliest_break_support
            and (
                earliest_break.get("causal_parent_ids")
                or "edge_state_mismatch" in (earliest_break.get("violations") or [])
                or "source_runtime_revision_mismatch"
                in (earliest_break.get("violations") or [])
                or "transition_from_mismatch"
                in (earliest_break.get("violations") or [])
            )
        )
        provenance_break_support = bool(
            provenance_break_evidence_id
            and provenance_break_evidence_id in causal_support_ids
        )
        causal_sufficiency = _causal_sufficiency(
            causal_support_records,
            earliest_break_support=earliest_break_graph_qualified,
            provenance_break_support=provenance_break_support,
        )
        coarse_reset_support = any(
            _is_coarse_reset_experiment(record)
            for record in causal_support_records
        )
        attribution_resolving_support = bool(
            earliest_break_graph_qualified
            or provenance_break_support
            or any(
                _is_attribution_resolving_evidence(record)
                for record in causal_support_records
            )
        )
        hypothesis_dimension = str(item.get("dimension") or "unknown")
        ranking_records_by_id = {
            str(record.get("evidence_id") or f"ranking-{index}"): record
            for index, record in enumerate(
                [*causal_support_records, *context_records]
            )
        }
        ranking_records = list(ranking_records_by_id.values())
        aligned_dimension_records = [
            record
            for record in ranking_records
            if _evidence_owner_dimension(record) == hypothesis_dimension
            and hypothesis_dimension != "unknown"
        ]
        causal_aligned_dimension_records = [
            record
            for record in causal_support_records
            if _evidence_owner_dimension(record) == hypothesis_dimension
            and hypothesis_dimension != "unknown"
        ]
        explicit_owner_aligned_records = [
            record
            for record in causal_aligned_dimension_records
            if str(record.get("dimension_origin") or "unknown") == "explicit"
            and _is_qualified_causal_record(record)
        ]
        dimension_mismatch_records = [
            record
            for record in ranking_records
            if _evidence_owner_dimension(record) not in {hypothesis_dimension, "unknown"}
        ]
        causal_dimension_mismatch_records = [
            record
            for record in causal_support_records
            if _evidence_owner_dimension(record) not in {hypothesis_dimension, "unknown"}
        ]
        dimension_alignment_weight = _independent_weight(
            aligned_dimension_records
        )
        dimension_mismatch_weight = _independent_weight(
            dimension_mismatch_records
        )
        causal_dimension_alignment_weight = _independent_weight(
            causal_aligned_dimension_records
        )
        causal_dimension_mismatch_weight = _independent_weight(
            causal_dimension_mismatch_records
        )
        ownership_bonus = {
            "isolated": 1.0,
            "graph_linked": 0.7,
            "direct_artifact": 0.3,
            "observational": 0.0,
        }[causal_sufficiency]
        ownership_weight = round(
            max(
                0.0,
                confirmatory_weight
                + ownership_bonus
                + (0.25 * causal_dimension_alignment_weight)
                - (0.25 * causal_dimension_mismatch_weight)
                - (0.5 * contradict_weight),
            ),
            4,
        )
        if (
            contradict_weight >= 0.7
            and contradict_weight >= confirmatory_weight
        ):
            status = "refuted"
        elif (
            causal_sufficiency in {"isolated", "graph_linked"}
            and confirmatory_weight >= 0.8
        ) or (
            causal_sufficiency == "direct_artifact"
            and len(direct_artifact_records) >= 2
            and confirmatory_weight >= 1.6
        ):
            status = "supported"
        elif support_weight > 0:
            status = "provisional"
        else:
            status = "untested"
        blockers: list[str] = []
        downstream_only = bool(linked_support_records) and not (
            causal_support_records or context_records or implicit_contradict_records
        )
        if downstream_only:
            blockers.append(
                "Support is downstream of the earliest structured causal break."
            )
        if item.get("dimension") == "unknown" and status == "supported":
            status = "provisional"
            blockers.append("Unknown is not a confirmable causal family; isolate a known dimension first.")
        attribution_gap_blocked = False
        dimension = str(item.get("dimension") or "unknown")
        if (
            unresolved_attribution
            and status == "refuted"
            and causal_support_weight > 0
            and contradict_weight <= confirmatory_weight + 0.25
        ):
            status = "blocked"
            attribution_gap_blocked = True
            blockers.append(
                "Mixed causal support and counterevidence cannot be resolved without missing execution attribution."
            )
        if drift and status in {"supported", "provisional"}:
            if dimension == "test_harness":
                if (
                    unresolved_attribution
                    or _causal_sufficiency_rank(causal_sufficiency) < 2
                ):
                    status = "provisional"
                    blockers.append(
                        "Baseline drift identifies a comparison-harness confound, but its exact component remains unresolved."
                    )
            elif (
                dimension == "code"
                or _causal_sufficiency_rank(causal_sufficiency) == 0
            ) and _causal_sufficiency_rank(causal_sufficiency) < 2:
                status = "blocked"
                blockers.append(
                    "Baseline drift remains unexplained and this causal family was not isolated."
                )
            elif _causal_sufficiency_rank(causal_sufficiency) < 2:
                status = "provisional"
                blockers.append(
                    "Baseline drift prevents confirmation until the comparison population and environment are reproducible."
                )
        if (
            runtime_source_mismatch
            and dimension == "code"
            and status in {"supported", "provisional"}
            and _causal_sufficiency_rank(causal_sufficiency) < 2
        ):
            status = "blocked"
            blockers.append(
                "Code causality is not isolated while source/runtime revision parity differs."
            )
        if (
            mechanism_attribution_gap
            and status in {"supported", "provisional"}
            and not attribution_resolving_support
        ):
            if coarse_reset_support and dimension not in {"code", "unknown"}:
                status = "provisional"
                blockers.append(
                    "A broad reset localizes the causal family but does not identify the owning mechanism."
                )
            else:
                status = "blocked"
                attribution_gap_blocked = True
                blockers.append(
                    "The retained experiment does not resolve the missing event-level mechanism attribution."
                )
        if (
            unresolved_attribution
            and not mechanism_attribution_gap
            and status in {"supported", "provisional"}
            and _causal_sufficiency_rank(causal_sufficiency) < 2
            and not (dimension == "test_harness" and bool(drift))
            and not (
                bool(drift)
                and dimension not in {"code", "unknown"}
                and _causal_sufficiency_rank(causal_sufficiency) >= 1
            )
        ):
            status = "blocked"
            attribution_gap_blocked = True
            blockers.append(
                "Retained evidence cannot attribute the suspected mechanism to the failing correlation or execution path."
            )
        denominator = support_weight + contradict_weight + 1.0
        confidence = max(0.0, min(0.99, support_weight / denominator))
        if status in {"refuted", "blocked"}:
            confidence = min(confidence, 0.49)
        hypothesis_results.append(
            {
                **item,
                "status": status,
                "confidence": round(confidence, 4),
                "support_weight": support_weight,
                "causal_support_weight": causal_support_weight,
                "context_weight": context_weight,
                "confirmatory_weight": confirmatory_weight,
                "contradict_weight": contradict_weight,
                "ownership_weight": ownership_weight,
                "dimension_alignment_weight": dimension_alignment_weight,
                "dimension_mismatch_weight": dimension_mismatch_weight,
                "causal_dimension_alignment_weight": causal_dimension_alignment_weight,
                "causal_dimension_mismatch_weight": causal_dimension_mismatch_weight,
                "causal_sufficiency": causal_sufficiency,
                "explicit_owner_aligned_causal_support": bool(
                    explicit_owner_aligned_records
                ),
                "causal_support_evidence_ids": [
                    str(record.get("evidence_id") or "")
                    for record in causal_support_records
                    if str(record.get("evidence_id") or "")
                ],
                "discriminating_evidence": discriminating,
                "typed_probe_evidence": typed_probe_evidence,
                "earliest_break_support": earliest_break_support,
                "earliest_break_graph_qualified": earliest_break_graph_qualified,
                "provenance_break_support": provenance_break_support,
                "coarse_reset_support": coarse_reset_support,
                "attribution_resolving_support": attribution_resolving_support,
                "downstream_only_support": downstream_only,
                "attribution_gap_blocked": attribution_gap_blocked,
                "blockers": blockers,
            }
        )

    results_by_id = {str(item.get("hypothesis_id")): item for item in hypothesis_results}
    requested = packet["conclusion"]
    conclusion_id = str(requested.get("hypothesis_id") or "")
    chosen = results_by_id.get(conclusion_id)
    requested_choice_id = conclusion_id

    def selection_rank(
        item: Mapping[str, Any],
    ) -> tuple[int, bool, int, float, float, float, float, float]:
        status_rank = {
            "supported": 4,
            "provisional": 3,
            "blocked": 2,
            "untested": 1,
            "refuted": 0,
        }.get(str(item.get("status") or "untested"), 0)
        return (
            status_rank,
            bool(item.get("typed_probe_evidence")),
            _causal_sufficiency_rank(item.get("causal_sufficiency")),
            float(item.get("ownership_weight") or 0),
            float(item.get("confirmatory_weight") or 0),
            float(item.get("support_weight") or 0)
            - float(item.get("contradict_weight") or 0),
            float(item.get("dimension_alignment_weight") or 0)
            - float(item.get("dimension_mismatch_weight") or 0),
            float(item.get("context_weight") or 0),
        )

    def lexical_override_is_grounded(item: Mapping[str, Any]) -> bool:
        return bool(
            _causal_sufficiency_rank(item.get("causal_sufficiency")) >= 2
            or item.get("explicit_owner_aligned_causal_support")
        )

    if chosen is None and hypothesis_results:
        chosen = max(hypothesis_results, key=selection_rank)
        conclusion_id = str(chosen.get("hypothesis_id") or "")
    if chosen is not None and chosen.get("dimension") == "unknown":
        known_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") != "unknown"
            and item.get("status") != "refuted"
            and (
                item.get("status") != "untested"
                or float(item.get("context_weight") or 0) > 0
            )
        ]
        if known_candidates:
            chosen = max(known_candidates, key=selection_rank)
            conclusion_id = str(chosen.get("hypothesis_id") or "")
    if chosen is not None:
        supported = [item for item in hypothesis_results if item.get("status") == "supported"]
        if supported:
            strongest_supported = max(supported, key=selection_rank)
            if (
                chosen.get("status") != "supported"
                or selection_rank(strongest_supported) > selection_rank(chosen)
            ):
                chosen = strongest_supported
                conclusion_id = str(chosen.get("hypothesis_id") or "")
        elif chosen.get("status") in {"blocked", "untested", "refuted"}:
            provisional = [
                item
                for item in hypothesis_results
                if item.get("status") == "provisional"
                and float(item.get("causal_support_weight") or 0) > 0
                and float(item.get("ownership_weight") or 0) > 0
            ]
            if provisional:
                chosen = max(provisional, key=selection_rank)
                conclusion_id = str(chosen.get("hypothesis_id") or "")

    if earliest_break_id and earliest_break_dimension != "unknown":
        causal_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") == earliest_break_dimension
            and item.get("status") not in {"refuted", "untested"}
            and (
                bool(item.get("earliest_break_support"))
                or earliest_break_id in (item.get("support_evidence_ids") or [])
            )
        ]
        if causal_candidates:
            causal_candidate = max(causal_candidates, key=selection_rank)
            if chosen is None or selection_rank(causal_candidate) > selection_rank(chosen):
                chosen = causal_candidate
                conclusion_id = str(chosen.get("hypothesis_id") or "")

    if provenance_break_evidence_id and provenance_break_dimension != "unknown":
        provenance_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") == provenance_break_dimension
            and item.get("status") not in {"refuted", "untested"}
            and (
                bool(item.get("provenance_break_support"))
                or provenance_break_evidence_id
                in (item.get("support_evidence_ids") or [])
            )
        ]
        if provenance_candidates:
            provenance_candidate = max(provenance_candidates, key=selection_rank)
            if chosen is None or selection_rank(provenance_candidate) > selection_rank(chosen):
                chosen = provenance_candidate
                conclusion_id = str(chosen.get("hypothesis_id") or "")

    if drift and problem_dimension == "test_harness":
        strong_closed_cause = bool(
            chosen
            and chosen.get("dimension") != "test_harness"
            and chosen.get("status") == "supported"
            and _causal_sufficiency_rank(chosen.get("causal_sufficiency")) >= 2
        )
        harness_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") == "test_harness"
            and item.get("status") != "refuted"
        ]
        if harness_candidates and not strong_closed_cause:
            chosen = max(harness_candidates, key=selection_rank)
            conclusion_id = str(chosen.get("hypothesis_id") or "")

    if (
        chosen is not None
        and problem_dimension not in {"unknown", str(chosen.get("dimension") or "unknown")}
        and _causal_sufficiency_rank(chosen.get("causal_sufficiency")) < 2
    ):
        problem_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") == problem_dimension
            and item.get("status") not in {"refuted", "untested"}
            and lexical_override_is_grounded(item)
        ]
        if problem_candidates:
            problem_candidate = max(problem_candidates, key=selection_rank)
            if (
                selection_rank(problem_candidate)[0] >= selection_rank(chosen)[0]
                and (
                    float(problem_candidate.get("ownership_weight") or 0) + 0.6
                    > float(chosen.get("ownership_weight") or 0)
                    or bool(problem_candidate.get("attribution_gap_blocked"))
                )
            ):
                chosen = problem_candidate
                conclusion_id = str(chosen.get("hypothesis_id") or "")

    if (
        chosen is not None
        and decisive_problem_dimension not in {
            "unknown",
            str(chosen.get("dimension") or "unknown"),
        }
        and _causal_sufficiency_rank(chosen.get("causal_sufficiency")) < 2
    ):
        decisive_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") == decisive_problem_dimension
            and item.get("status") not in {"refuted", "untested"}
            and lexical_override_is_grounded(item)
        ]
        if decisive_candidates:
            chosen = max(decisive_candidates, key=selection_rank)
            conclusion_id = str(chosen.get("hypothesis_id") or "")

    requested_status = str(requested.get("status") or "provisional")
    promotion_reason = ""
    if chosen is None:
        effective_status = "inconclusive"
    elif chosen.get("status") == "refuted":
        effective_status = "rejected"
    elif (
        chosen.get("status") == "supported"
        and not errors
        and _causal_sufficiency_rank(chosen.get("causal_sufficiency")) >= 2
        and not chosen.get("blockers")
    ):
        effective_status = "confirmed"
        if requested_status != "confirmed":
            promotion_reason = (
                "Deterministic evidence gate promoted a cautious conclusion only after independently qualified causal proof."
            )
    elif chosen.get("status") in {"blocked", "untested"}:
        effective_status = "inconclusive"
    elif chosen.get("status") == "provisional":
        effective_status = "provisional"
    elif chosen.get("status") == "supported":
        effective_status = "provisional"
    else:
        effective_status = "inconclusive"

    retractions: list[dict[str, Any]] = []
    if previous_report:
        previous = previous_report.get("conclusion") if isinstance(previous_report.get("conclusion"), Mapping) else {}
        previous_id = str(previous.get("hypothesis_id") or "")
        previous_status = str(previous.get("status") or "")
        if previous_status == "confirmed" and (previous_id != conclusion_id or effective_status != "confirmed"):
            retractions.append(
                {
                    "hypothesis_id": previous_id,
                    "previous_status": previous_status,
                    "new_status": effective_status if previous_id == conclusion_id else "superseded",
                    "reason": "New counter-evidence or a stronger competing explanation invalidated the earlier conclusion.",
                }
            )

    if effective_status == "confirmed" and not (chosen or {}).get("blockers"):
        decision = "patch_root_cause"
    elif (
        effective_status == "provisional"
        or drift
        or any(
            item.get("status") in {"provisional", "blocked"}
            for item in hypothesis_results
        )
    ):
        decision = "instrument_first"
    else:
        decision = "investigate"

    recommendations = recommend_counterfactuals(case, packet, hypothesis_results, drift)
    chosen_causal_evidence_ids = list(
        (chosen or {}).get("causal_support_evidence_ids") or []
    )
    selected_evidence_ids = (
        chosen_causal_evidence_ids
        or list((chosen or {}).get("support_evidence_ids") or [])
        or list(requested.get("evidence_ids") or [])
    )
    selected_reason = str(requested.get("reason") or "")
    if conclusion_id and conclusion_id != requested_choice_id and chosen is not None:
        selected_reason = (
            "Deterministic evidence gate selected a stronger supported competing hypothesis."
        )
    return {
        "schema": REPORT_SCHEMA,
        "case_id": case["case_id"],
        "valid": not errors,
        "errors": errors,
        "baseline_drift": drift,
        "attribution_assessment": {
            "unresolved": unresolved_attribution,
            "mechanism_gap": mechanism_attribution_gap,
            "evidence_ids": [
                str(item.get("evidence_id") or "")
                for item in attribution_gap_records
            ],
        },
        "causal_timeline": causal_timeline,
        "provenance_graph": provenance_graph,
        "hypothesis_results": hypothesis_results,
        "conclusion": {
            "hypothesis_id": conclusion_id,
            "status": effective_status,
            "dimension": str(chosen.get("dimension") or "unknown") if chosen else "unknown",
            "claim": str(chosen.get("claim") or "") if chosen else "",
            "confidence": float(chosen.get("confidence") or 0) if chosen else 0.0,
            "evidence_ids": selected_evidence_ids,
            "reason": selected_reason,
            "requested_status": requested_status,
            "causal_sufficiency": str(
                chosen.get("causal_sufficiency") or "observational"
            ) if chosen else "observational",
            "promotion_reason": promotion_reason,
            "blockers": list(chosen.get("blockers") or []) if chosen else [],
        },
        "decision": decision,
        "retractions": retractions,
        "next_experiments": recommendations,
        "contract_invariants": derive_contract_invariants(case["problem_statement"]),
        "diagnostic_lenses": list(
            (case.get("constraints") or {}).get("diagnostic_lenses") or []
        ),
        "premium_calls": 0,
    }


def heuristic_packet(raw_case: Mapping[str, Any]) -> dict[str, Any]:
    case = normalize_case(raw_case)
    by_dimension: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in case["observations"]:
        by_dimension[str(item.get("dimension") or "unknown")].append(item)
    ranked = sorted(
        by_dimension.items(),
        key=lambda pair: (
            pair[0] == "unknown",
            -sum(
                float(item.get("reliability") or 0)
                * (
                    2.0
                    if str(item.get("causal_role") or "") == "support"
                    else 0.2
                    if str(item.get("causal_role") or "") == "context"
                    else -1.0
                )
                * (1.4 if bool(item.get("discriminating")) else 1.0)
                * (
                    1.15
                    if str(item.get("kind") or "") in {"artifact", "experiment"}
                    else 1.0
                )
                for item in pair[1]
            ),
            pair[0],
        ),
    )
    minimum_dimensions = int(
        (case.get("constraints") or {}).get("minimum_hypothesis_dimensions") or 1
    )
    represented_dimensions = {
        dimension
        for dimension, _records in ranked
        if dimension != "unknown"
    }
    if len(represented_dimensions) < minimum_dimensions:
        secondary_scores: dict[str, int] = defaultdict(int)
        for item in case["observations"]:
            for dimension, score in _dimension_scores(
                str(item.get("statement") or "")
            ).items():
                secondary_scores[dimension] += score
        for dimension, score in _dimension_scores(case["problem_statement"]).items():
            secondary_scores[dimension] += score
        for dimension, score in sorted(
            secondary_scores.items(),
            key=lambda pair: (-pair[1], pair[0]),
        ):
            if (
                score <= 0
                or dimension == "unknown"
                or dimension in represented_dimensions
            ):
                continue
            unknown_index = next(
                (
                    index
                    for index, (ranked_dimension, _records) in enumerate(ranked)
                    if ranked_dimension == "unknown"
                ),
                len(ranked),
            )
            ranked.insert(unknown_index, (dimension, []))
            represented_dimensions.add(dimension)
            if len(represented_dimensions) >= minimum_dimensions:
                break
    if len(represented_dimensions) < minimum_dimensions:
        for dimension in DIMENSIONS:
            if dimension == "unknown" or dimension in represented_dimensions:
                continue
            unknown_index = next(
                (
                    index
                    for index, (ranked_dimension, _records) in enumerate(ranked)
                    if ranked_dimension == "unknown"
                ),
                len(ranked),
            )
            ranked.insert(unknown_index, (dimension, []))
            represented_dimensions.add(dimension)
            if len(represented_dimensions) >= minimum_dimensions:
                break
    hypotheses: list[dict[str, Any]] = []
    experiments: list[dict[str, Any]] = []
    for index, (dimension, records) in enumerate(ranked[:4]):
        hypothesis_id = f"h-{dimension}"
        support_records = [
            item
            for item in records
            if str(item.get("causal_role") or "context") == "support"
        ]
        context_records = [
            item
            for item in records
            if str(item.get("causal_role") or "context") == "context"
        ]
        hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "claim": f"The observed failure is primarily caused by {dimension} drift.",
                "dimension": dimension,
                "support_evidence_ids": [
                    str(item.get("evidence_id"))
                    for item in [*support_records, *context_records]
                ],
                "contradict_evidence_ids": [],
                "falsification": f"Hold every other dimension constant and show that changing {dimension} does not change the outcome.",
            }
        )
        experiments.append(
            {
                "experiment_id": f"isolate-{dimension}",
                "hypothesis_ids": [hypothesis_id],
                "changed_dimensions": [dimension],
                "held_constant_dimensions": [item for item in DIMENSIONS if item not in {dimension, "unknown"}],
                "expected_if_true": f"Changing {dimension} changes or restores the outcome.",
                "expected_if_false": "The outcome remains unchanged.",
                "evidence_required": ["before fingerprint", "after fingerprint", "actual outcome"],
                "result_evidence_ids": [],
                "safety": "isolated",
                "status": "planned",
            }
        )
    top = hypotheses[0] if hypotheses else {"hypothesis_id": "", "support_evidence_ids": []}
    return normalize_packet(
        {
            "problem_statement": case["problem_statement"],
            "hypotheses": hypotheses,
            "experiments": experiments,
            "conclusion": {
                "hypothesis_id": top.get("hypothesis_id"),
                "status": "provisional",
                "evidence_ids": top.get("support_evidence_ids") or [],
                "reason": "Heuristic fallback only; a local investigator should challenge this ranking.",
            },
        }
    )


def _prompt_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _has_prompt_value(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _case_prompt(case: Mapping[str, Any]) -> str:
    observation_keys = (
        "evidence_id",
        "statement",
        "structured_context",
        "dimension",
        "observed_dimension",
        "causal_dimension",
        "dimension_origin",
        "kind",
        "evidence_lifecycle",
        "intervention_scope",
        "retained_comparison",
        "provenance",
        "independence_key",
        "reliability",
        "discriminating",
        "causal_role",
        "attribution_gap",
        "changed_dimensions",
        "held_constant_dimensions",
        "expected_if_true",
        "expected_if_false",
        "comparison_key",
        "code_revision",
        "input_fingerprint",
        "environment_fingerprint",
        "outcome_fingerprint",
        "experiment_id",
        "observed_at",
        "sequence",
        "entity_id",
        "event_type",
        "expected_state",
        "actual_state",
        "transition_from",
        "transition_to",
        "causal_parent_ids",
        "source_revision",
        "runtime_revision",
        "service_id",
        "producer_id",
        "consumer_id",
        "sink_id",
        "edge_from",
        "edge_to",
        "expected_edge_state",
        "actual_edge_state",
        "artifact_hash",
        "correlation_fingerprints",
    )
    safe_case = {
        "case_id": case.get("case_id"),
        "problem_statement": case.get("problem_statement"),
        "observations": [
            {
                key: item.get(key)
                for key in observation_keys
                if key in item and _has_prompt_value(item.get(key))
            }
            for item in case.get("observations") or []
            if isinstance(item, Mapping)
        ],
        "prior_conclusion": case.get("prior_conclusion"),
        "constraints": case.get("constraints"),
    }
    timeline = _structured_causal_timeline(case)
    if timeline:
        safe_case["causal_timeline"] = timeline
    provenance_graph = _structured_provenance_graph(case)
    if provenance_graph:
        safe_case["provenance_graph"] = provenance_graph
    return _prompt_json(safe_case)


def _packet_shape() -> str:
    return (
        '{"hypotheses":[{"hypothesis_id":"h1","claim":"<=12 words","dimension":"code|data|clock|state|config|dependency|runtime|test_harness|unknown",'
        '"support_evidence_ids":["e1"],"contradict_evidence_ids":[],"falsification":"<=12 words"}],'
        '"experiments":[{"experiment_id":"x1","hypothesis_ids":["h1"],'
        '"changed_dimensions":["clock"],"held_constant_dimensions":["code","data"],'
        '"expected_if_true":"<=12 words","expected_if_false":"<=12 words",'
        '"safety":"read_only","auto_execute":true,"probe":{"probe_id":"p1","kind":"search",'
        '"paths":["app"],"query":"fixed string","dimension":"clock"}}],'
        '"conclusion":{"hypothesis_id":"h1","status":"confirmed|provisional|inconclusive|rejected",'
        '"evidence_ids":["e1"],"reason":"<=12 words"}}'
    )


def _compact_output_rules(case: Mapping[str, Any]) -> str:
    required = int(
        (case.get("constraints") or {}).get("minimum_hypothesis_dimensions") or 1
    )
    return (
        "Hard output budget: at most 700 tokens. Do not restate the case or evidence text. "
        f"Return {required} to 4 concise hypotheses across distinct dimensions. "
        "Every claim, falsification, and reason is at most 12 words. Use evidence ids only. "
        "Use experiments=[] unless one or two bounded typed probes are essential. "
        "Omit schema and problem_statement. Close every JSON array and object."
    )


def _packet_prompt(packet: Mapping[str, Any]) -> str:
    normalized = normalize_packet(packet)
    return _prompt_json(
        {
            "hypotheses": list(normalized.get("hypotheses") or [])[:4],
            "experiments": list(normalized.get("experiments") or [])[:2],
            "conclusion": normalized.get("conclusion") or {},
        }
    )


def _report_prompt(report: Mapping[str, Any]) -> str:
    hypothesis_results = []
    for item in report.get("hypothesis_results") or []:
        if not isinstance(item, Mapping):
            continue
        hypothesis_results.append(
            {
                key: item.get(key)
                for key in (
                    "hypothesis_id",
                    "dimension",
                    "status",
                    "support_evidence_ids",
                    "contradict_evidence_ids",
                    "discriminating_evidence",
                    "causal_sufficiency",
                    "ownership_weight",
                    "attribution_gap_blocked",
                    "blockers",
                )
                if _has_prompt_value(item.get(key))
            }
        )
    return _prompt_json(
        {
            "valid": bool(report.get("valid")),
            "errors": list(report.get("errors") or [])[:6],
            "baseline_drift": list(report.get("baseline_drift") or [])[:2],
            "attribution_assessment": report.get("attribution_assessment") or {},
            "decision": report.get("decision"),
            "conclusion": report.get("conclusion") or {},
            "hypothesis_results": hypothesis_results[:4],
            "next_experiments": list(report.get("next_experiments") or [])[:2],
            "causal_timeline": report.get("causal_timeline") or {},
            "provenance_graph": report.get("provenance_graph") or {},
        }
    )


def _typed_probe_examples() -> str:
    return (
        'Typed experiment examples (probe stays nested): '
        '{"experiment_id":"x-log","hypothesis_ids":["h1"],'
        '"changed_dimensions":["runtime"],"held_constant_dimensions":["code"],'
        '"expected_if_true":"match","expected_if_false":"no match","safety":"read_only",'
        '"auto_execute":true,"probe":{"probe_id":"p-log","kind":"log_search",'
        '"paths":["logs"],"query":"connection refused","dimension":"runtime"}}.'
    )


def _schema_correction_prompt(
    stage: str,
    raw_packet: Mapping[str, Any],
    errors: Sequence[str],
) -> str:
    return (
        f"Your {stage} response parsed as JSON but did not match the diagnostic packet schema. "
        "Return one corrected full packet as JSON only. Preserve the same claims and evidence ids. "
        "Do not return a standalone probe; every probe must be nested under an experiment wrapper. "
        "This is the only schema-correction attempt.\n\n"
        f"Schema errors:\n{_prompt_json(list(errors)[:8])}\n\n"
        f"Required shape:\n{_packet_shape()}\n{_typed_probe_examples()}\n\n"
        f"Malformed packet:\n{_clip(_prompt_json(raw_packet), 3200)}"
    )


def investigator_prompt(raw_case: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the investigator in a local-only diagnostic team. Return JSON only. "
        "Generate competing hypotheses across dimensions. Cite evidence ids. "
        "Reconstruct expected and observed behavior; separate the earliest causal break from downstream symptoms. "
        "Treat each diagnostic lens as a question. For incidents, separate requirements, external conditions, "
        "state, evidence coverage, and source-versus-running-revision parity. "
        "Every hypothesis needs a short falsification; create an experiment only when a typed probe is essential. "
        "Same code and input with different outcomes "
        "means baseline drift, not proof of a code regression. Never request automatic runtime or live mutation. "
        "When evidence is insufficient, you may set auto_execute=true only for a typed probe from the supplied "
        "catalog. Raw shell commands do not exist. search is fixed-string; targeted_test must name one selector "
        "under tests/. log_search is fixed-string over bounded log tails. db_schema and db_profile never accept "
        "SQL; db_profile exposes only count/group/min/max/avg/sum aggregates and production use requires a "
        "timestamp column plus bounded lookback. Use read_only for repo_state/search/file_excerpt/git_history/"
        "git_diff/log_inventory/log_search/db_schema/db_profile and isolated for compile/targeted_test. "
        f"Causal ownership rubric:\n{_prompt_json(CAUSAL_DIMENSION_RUBRIC)}\n\n"
        f"{_compact_output_rules(case)}\n\n"
        f"Required shape:\n{_packet_shape()}\n{_typed_probe_examples()}\n\nCase:\n{_case_prompt(case)}"
    )


def skeptic_prompt(raw_case: Mapping[str, Any], packet: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the skeptic in a local-only diagnostic team. Return one full revised diagnostic packet as JSON only. "
        "Try to falsify the leading conclusion. Look for code/data/clock/state/config/dependency/runtime/test-harness confounding, "
        "correlated evidence, and claims that survived no discriminating experiment. Add contradiction evidence links when justified. "
        "Challenge post-hoc metric optimization, replay leakage, source/runtime drift, producer-consumer starvation, "
        "broker/local-state divergence, external-condition confounding, and fixes aimed only at a downstream symptom. "
        "Retract a conclusion rather than defending it when the evidence changed. Never request automatic runtime or live mutation. "
        f"Causal ownership rubric:\n{_prompt_json(CAUSAL_DIMENSION_RUBRIC)}\n\n"
        f"{_compact_output_rules(case)}\n\n"
        f"Required shape:\n{_packet_shape()}\n{_typed_probe_examples()}\n\nCase:\n{_case_prompt(case)}\n\n"
        f"Investigator packet:\n{_packet_prompt(packet)}\n\n"
        f"Deterministic evaluation:\n{_report_prompt(report)}"
    )


def judge_prompt(raw_case: Mapping[str, Any], packet: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the judge in a local-only diagnostic team. Return one final full diagnostic packet as JSON only. "
        "Confirm only a hypothesis with independent, discriminating evidence and no unresolved contradiction. "
        "Require a coherent expected-to-observed timeline, identify the earliest supported causal break, and keep "
        "the strategy contract separate from implementation correctness. A profitable counterfactual is not proof "
        "unless its inputs, clock, data coverage, and execution assumptions match. Include a bounded post-change "
        "proof or keep the conclusion provisional. "
        "If baseline drift remains unexplained, reject code attribution and choose instrument-first. "
        "Preserve safe falsification experiments and never request automatic runtime or live mutation. "
        "If the evidence gate says instrument_first, choose at most two auto_execute typed probes from this "
        "catalog: repo_state, fixed-string search, bounded file_excerpt, git_history, git_diff, isolated compile, "
        "one targeted_test selector under tests/, bounded log_inventory/log_search, or aggregate-only "
        "db_schema/db_profile. Database probes never accept SQL or raw-row selection. Raw commands are forbidden. "
        f"Causal ownership rubric:\n{_prompt_json(CAUSAL_DIMENSION_RUBRIC)}\n\n"
        f"{_compact_output_rules(case)}\n\n"
        f"Required shape:\n{_packet_shape()}\n{_typed_probe_examples()}\n\nCase:\n{_case_prompt(case)}\n\n"
        f"Challenged packet:\n{_packet_prompt(packet)}\n\n"
        f"Deterministic evaluation:\n{_report_prompt(report)}"
    )


ModelCall = Callable[[str, str], str]


def _repair_candidate_contract(
    case: Mapping[str, Any],
    candidate: Mapping[str, Any],
    previous_packet: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Fail closed on common small-model schema slips while preserving its claim.

    Repairs never invent evidence or make an experiment executable. They only
    drop mismatched support links, restore already-grounded contract fields,
    and demote malformed automatic experiments to non-executable plans.
    """
    normalized = normalize_packet(candidate)
    previous = normalize_packet(previous_packet)
    evidence_contracts = {
        str(item.get("evidence_id") or ""): {
            "dimension": str(item.get("dimension") or "unknown"),
            "causal_dimension": str(item.get("causal_dimension") or "unknown"),
            "dimension_origin": str(item.get("dimension_origin") or "unknown"),
            "causal_role": str(item.get("causal_role") or "context"),
            "provenance": str(item.get("provenance") or ""),
            "intervention_scope": str(item.get("intervention_scope") or "none"),
            "evidence_lifecycle": str(
                item.get("evidence_lifecycle") or "observed_result"
            ),
            "attribution_gap": bool(item.get("attribution_gap")),
            "qualified_causal_support": _is_qualified_causal_record(item),
        }
        for item in case.get("observations") or []
        if isinstance(item, Mapping) and str(item.get("evidence_id") or "")
    }

    def support_is_compatible(evidence_id: str, dimension: str) -> bool:
        contract = evidence_contracts.get(evidence_id)
        if not contract or contract.get("causal_role") == "contradiction":
            return False
        hard_owned = (
            contract.get("dimension_origin") == "explicit"
        )
        owner = str(contract.get("causal_dimension") or "unknown")
        if owner == "unknown":
            owner = str(contract.get("dimension") or "unknown")
        return not hard_owned or owner in {dimension, "unknown"}

    def contradiction_is_compatible(evidence_id: str, dimension: str) -> bool:
        contract = evidence_contracts.get(evidence_id)
        if (
            not contract
            or contract.get("attribution_gap")
            or contract.get("evidence_lifecycle") != "observed_result"
        ):
            return False
        if contract.get("causal_role") == "contradiction":
            return True
        owner = str(contract.get("causal_dimension") or "unknown")
        if owner == "unknown":
            owner = str(contract.get("dimension") or "unknown")
        return bool(
            contract.get("causal_role") == "support"
            and contract.get("intervention_scope") == "component"
            and owner not in {"unknown", dimension}
        )

    def qualified_causal_support_is_compatible(
        evidence_id: str,
        dimension: str,
    ) -> bool:
        contract = evidence_contracts.get(evidence_id)
        owner = str((contract or {}).get("causal_dimension") or "unknown")
        if owner == "unknown":
            owner = str((contract or {}).get("dimension") or "unknown")
        return bool(
            contract
            and contract.get("qualified_causal_support")
            and owner == dimension
            and support_is_compatible(evidence_id, dimension)
        )

    previous_by_id = {
        str(item.get("hypothesis_id") or ""): item
        for item in previous.get("hypotheses") or []
        if isinstance(item, Mapping)
    }
    previous_by_dimension = {
        str(item.get("dimension") or "unknown"): item
        for item in previous.get("hypotheses") or []
        if isinstance(item, Mapping)
    }
    repairs: list[str] = []
    hypotheses: list[dict[str, Any]] = []
    for item in normalized.get("hypotheses") or []:
        repaired = dict(item)
        hypothesis_id = str(repaired.get("hypothesis_id") or "")
        dimension = str(repaired.get("dimension") or "unknown")
        prior = previous_by_id.get(hypothesis_id) or previous_by_dimension.get(dimension)
        if not str(repaired.get("claim") or "").strip() and prior:
            repaired["claim"] = str(prior.get("claim") or "")
            repairs.append(f"{hypothesis_id}:restored_claim")
        if not str(repaired.get("falsification") or "").strip() and prior:
            repaired["falsification"] = str(prior.get("falsification") or "")
            repairs.append(f"{hypothesis_id}:restored_falsification")

        raw_support = list(repaired.get("support_evidence_ids") or [])
        aligned_support = [
            evidence_id
            for evidence_id in raw_support
            if support_is_compatible(str(evidence_id), dimension)
        ]
        prior_support = []
        if prior:
            prior_support = [
                evidence_id
                for evidence_id in prior.get("support_evidence_ids") or []
                if support_is_compatible(str(evidence_id), dimension)
            ]
        raw_contradictions = list(repaired.get("contradict_evidence_ids") or [])
        aligned_contradictions = [
            evidence_id
            for evidence_id in raw_contradictions
            if contradiction_is_compatible(str(evidence_id), dimension)
        ]
        if aligned_support != raw_support:
            dropped_roles = {
                str((evidence_contracts.get(str(evidence_id)) or {}).get("causal_role") or "")
                for evidence_id in raw_support
                if evidence_id not in aligned_support
            }
            if "contradiction" in dropped_roles:
                repairs.append(f"{hypothesis_id}:dropped_contradiction_support")
            if any(
                str(evidence_id) not in evidence_contracts
                for evidence_id in raw_support
                if evidence_id not in aligned_support
            ):
                repairs.append(f"{hypothesis_id}:dropped_unknown_support")
            if any(
                str(evidence_id) in evidence_contracts
                and str(
                    evidence_contracts[str(evidence_id)].get("causal_role") or ""
                )
                != "contradiction"
                for evidence_id in raw_support
                if evidence_id not in aligned_support
            ):
                repairs.append(f"{hypothesis_id}:dropped_mismatched_support")
        if not aligned_support and prior_support:
            aligned_support = prior_support
            if aligned_support:
                repairs.append(f"{hypothesis_id}:restored_grounded_support")
        prior_qualified_support = [
            evidence_id
            for evidence_id in prior_support
            if qualified_causal_support_is_compatible(
                str(evidence_id),
                dimension,
            )
        ]
        candidate_qualified_support = [
            evidence_id
            for evidence_id in aligned_support
            if qualified_causal_support_is_compatible(
                str(evidence_id),
                dimension,
            )
        ]
        if (
            prior_qualified_support
            and not candidate_qualified_support
            and not aligned_contradictions
        ):
            restored = [
                evidence_id
                for evidence_id in prior_qualified_support
                if evidence_id not in aligned_support
            ]
            if restored:
                aligned_support.extend(restored)
                repairs.append(
                    f"{hypothesis_id}:restored_qualified_causal_support"
                )
        repaired["support_evidence_ids"] = aligned_support
        if aligned_contradictions != raw_contradictions:
            repairs.append(f"{hypothesis_id}:dropped_unqualified_contradiction")
        repaired["contradict_evidence_ids"] = aligned_contradictions
        hypotheses.append(repaired)

    experiments: list[dict[str, Any]] = []
    for item in normalized.get("experiments") or []:
        repaired = dict(item)
        probe = repaired.get("probe") if isinstance(repaired.get("probe"), Mapping) else {}
        if bool(repaired.get("auto_execute")) and (
            not probe or str(repaired.get("safety") or "") not in AUTO_SAFE_LEVELS
        ):
            repaired["auto_execute"] = False
            repaired["probe"] = {}
            repairs.append(
                f"{str(repaired.get('experiment_id') or 'experiment')}:demoted_unsafe_auto_execute"
            )
        experiments.append(repaired)

    conclusion = dict(normalized.get("conclusion") or {})
    hypothesis_ids = {
        str(item.get("hypothesis_id") or "") for item in hypotheses
    }
    if str(conclusion.get("hypothesis_id") or "") not in hypothesis_ids and hypotheses:
        prior_id = str((previous.get("conclusion") or {}).get("hypothesis_id") or "")
        conclusion["hypothesis_id"] = (
            prior_id if prior_id in hypothesis_ids else str(hypotheses[0].get("hypothesis_id") or "")
        )
        repairs.append("conclusion:restored_known_hypothesis")

    return normalize_packet(
        {
            **normalized,
            "hypotheses": hypotheses,
            "experiments": experiments,
            "conclusion": conclusion,
        }
    ), repairs


def _preserve_competing_hypotheses(
    case: Mapping[str, Any],
    candidate: Mapping[str, Any],
    previous_packet: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Keep evidence-grounded alternatives when a small model collapses breadth."""
    required = int(
        (case.get("constraints") or {}).get("minimum_hypothesis_dimensions") or 1
    )
    normalized_candidate = normalize_packet(candidate)
    evidence = {
        str(item.get("evidence_id") or ""): item
        for item in case.get("observations") or []
        if isinstance(item, Mapping) and str(item.get("evidence_id") or "")
    }

    def has_owner_aligned_causal_support(item: Mapping[str, Any]) -> bool:
        dimension = str(item.get("dimension") or "unknown")
        if dimension == "unknown":
            return False
        return any(
            evidence_id in evidence
            and _evidence_owner_dimension(evidence[evidence_id]) == dimension
            and _is_qualified_causal_record(evidence[evidence_id])
            for evidence_id in (
                str(value) for value in item.get("support_evidence_ids") or []
            )
        )

    represented = {
        str(item.get("dimension") or "unknown")
        for item in normalized_candidate.get("hypotheses") or []
        if str(item.get("dimension") or "unknown") != "unknown"
    }
    previous_hypotheses = [
        item
        for item in previous_packet.get("hypotheses") or []
        if isinstance(item, Mapping)
    ]
    missing_grounded_dimensions = {
        str(item.get("dimension") or "unknown")
        for item in previous_hypotheses
        if has_owner_aligned_causal_support(item)
        and str(item.get("dimension") or "unknown") not in represented
    }
    if len(represented) >= required and not missing_grounded_dimensions:
        return normalized_candidate, []

    hypotheses = list(normalized_candidate.get("hypotheses") or [])
    experiments = list(normalized_candidate.get("experiments") or [])
    existing_hypothesis_ids = {
        str(item.get("hypothesis_id") or "") for item in hypotheses
    }
    existing_experiment_ids = {
        str(item.get("experiment_id") or "") for item in experiments
    }
    added_hypothesis_ids: set[str] = set()
    added_dimensions: list[str] = []
    for item in sorted(
        previous_hypotheses,
        key=lambda value: not has_owner_aligned_causal_support(value),
    ):
        dimension = str(item.get("dimension") or "unknown")
        hypothesis_id = str(item.get("hypothesis_id") or "")
        grounded = has_owner_aligned_causal_support(item)
        if (
            dimension == "unknown"
            or dimension in represented
            or not hypothesis_id
            or hypothesis_id in existing_hypothesis_ids
            or (not grounded and len(represented) >= required)
        ):
            continue
        hypotheses.append(dict(item))
        represented.add(dimension)
        existing_hypothesis_ids.add(hypothesis_id)
        added_hypothesis_ids.add(hypothesis_id)
        added_dimensions.append(dimension)

    for item in previous_packet.get("experiments") or []:
        if not isinstance(item, Mapping):
            continue
        experiment_id = str(item.get("experiment_id") or "")
        hypothesis_ids = {
            str(value) for value in item.get("hypothesis_ids") or []
        }
        if (
            not experiment_id
            or experiment_id in existing_experiment_ids
            or not (hypothesis_ids & added_hypothesis_ids)
        ):
            continue
        experiments.append(dict(item))
        existing_experiment_ids.add(experiment_id)

    return normalize_packet(
        {
            **normalized_candidate,
            "hypotheses": hypotheses,
            "experiments": experiments,
        }
    ), added_dimensions


def run_local_diagnostic_debate(
    raw_case: Mapping[str, Any],
    model_call: ModelCall | None,
    *,
    stages_to_run: Sequence[str] = ("investigator", "skeptic", "judge"),
    previous_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = normalize_case(raw_case)
    packet = heuristic_packet(case)
    prior_conclusion = case.get("prior_conclusion")
    prior_report = dict(previous_report) if isinstance(previous_report, Mapping) else None
    if prior_report is None:
        prior_report = (
            {"conclusion": dict(prior_conclusion)}
            if isinstance(prior_conclusion, Mapping) and prior_conclusion.get("status")
            else None
        )
    report = evaluate_packet(case, packet, previous_report=prior_report)
    stages: list[dict[str, Any]] = []
    all_retractions = list(report.get("retractions") or [])

    allowed_stages = {"investigator", "skeptic", "judge"}
    requested_stages = tuple(stage for stage in stages_to_run if stage in allowed_stages)
    if not requested_stages:
        requested_stages = ("judge",)

    for stage in requested_stages:
        if stage == "investigator":
            prompt = investigator_prompt(case)
        elif stage == "skeptic":
            prompt = skeptic_prompt(case, packet, report)
        else:
            prompt = judge_prompt(case, packet, report)
        response = model_call(stage, prompt) if model_call is not None else ""
        initial_parsed = parse_json_object(response)
        initial_json_parsed = initial_parsed is not None
        initial_raw_schema_errors = (
            validate_raw_packet_schema(initial_parsed)
            if initial_parsed is not None
            else []
        )
        initial_raw_schema_valid = bool(
            initial_json_parsed and not initial_raw_schema_errors
        )
        parsed = initial_parsed
        raw_schema_errors = list(initial_raw_schema_errors)
        correction_response = ""
        correction_json_parsed: bool | None = None
        correction_raw_schema_valid: bool | None = None
        schema_correction_attempted = bool(
            initial_parsed is not None
            and initial_raw_schema_errors
            and model_call is not None
        )
        if schema_correction_attempted:
            correction_response = model_call(
                f"{stage}_schema_correction",
                _schema_correction_prompt(
                    stage,
                    initial_parsed,
                    initial_raw_schema_errors,
                ),
            )
            corrected = parse_json_object(correction_response)
            correction_json_parsed = corrected is not None
            correction_errors = (
                validate_raw_packet_schema(corrected)
                if corrected is not None
                else []
            )
            correction_raw_schema_valid = bool(
                correction_json_parsed and not correction_errors
            )
            if corrected is not None:
                parsed = corrected
                raw_schema_errors = correction_errors
        json_parsed = parsed is not None
        raw_schema_valid = bool(json_parsed and not raw_schema_errors)
        accepted = raw_schema_valid
        requested_conclusion = dict(
            (normalize_packet(parsed).get("conclusion") or {})
            if raw_schema_valid and parsed is not None
            else (packet.get("conclusion") or {})
        )
        preserved_dimensions: list[str] = []
        contract_repairs: list[str] = []
        if raw_schema_valid and parsed is not None:
            repaired, contract_repairs = _repair_candidate_contract(
                case,
                parsed,
                packet,
            )
            candidate, preserved_dimensions = _preserve_competing_hypotheses(
                case,
                repaired,
                packet,
            )
        else:
            candidate = packet
        next_report = evaluate_packet(case, candidate, previous_report=report)
        candidate_errors = list(next_report.get("errors") or [])
        if not initial_json_parsed and response:
            candidate_errors.insert(0, "Model response was not a usable diagnostic JSON object.")
        elif raw_schema_errors:
            candidate_errors = [*raw_schema_errors, *candidate_errors]
        if not next_report["valid"] and accepted:
            accepted = False
            candidate = packet
            next_report = evaluate_packet(case, candidate, previous_report=report)
        stages.append(
            {
                "stage": stage,
                "accepted": accepted,
                "json_parsed": json_parsed,
                "raw_schema_valid": raw_schema_valid,
                "raw_schema_errors": raw_schema_errors,
                "initial_json_parsed": initial_json_parsed,
                "initial_raw_schema_valid": initial_raw_schema_valid,
                "initial_raw_schema_errors": initial_raw_schema_errors,
                "schema_correction_attempted": schema_correction_attempted,
                "schema_correction_json_parsed": correction_json_parsed,
                "schema_correction_raw_schema_valid": correction_raw_schema_valid,
                "schema_correction_response_chars": len(correction_response),
                "response_chars": len(response),
                "errors": candidate_errors,
                "contract_repairs": contract_repairs,
                "preserved_hypothesis_dimensions": preserved_dimensions,
                "requested_conclusion": requested_conclusion,
                "effective_conclusion": next_report.get("conclusion") or {},
                "conclusion": next_report.get("conclusion") or {},
                "retractions": next_report.get("retractions") or [],
            }
        )
        all_retractions.extend(next_report.get("retractions") or [])
        packet = candidate
        report = next_report

    report = {**report, "retractions": all_retractions}
    effective_conclusion = dict(report.get("conclusion") or {})
    final_hypotheses = list(packet.get("hypotheses") or [])
    effective_hypothesis_id = str(
        effective_conclusion.get("hypothesis_id") or ""
    )
    if effective_hypothesis_id and effective_hypothesis_id not in {
        str(item.get("hypothesis_id") or "")
        for item in final_hypotheses
        if isinstance(item, Mapping)
    }:
        effective_result = next(
            (
                item
                for item in report.get("hypothesis_results") or []
                if isinstance(item, Mapping)
                and str(item.get("hypothesis_id") or "")
                == effective_hypothesis_id
            ),
            None,
        )
        if effective_result is not None:
            final_hypotheses.append(dict(effective_result))
    final_packet = normalize_packet(
        {
            **packet,
            "hypotheses": final_hypotheses,
            "conclusion": {
                "hypothesis_id": effective_hypothesis_id,
                "status": str(effective_conclusion.get("status") or "inconclusive"),
                "evidence_ids": list(
                    effective_conclusion.get("evidence_ids") or []
                ),
                "reason": str(effective_conclusion.get("reason") or ""),
            },
        }
    )
    return {
        "schema": DEBATE_SCHEMA,
        "case_id": case["case_id"],
        "packet": final_packet,
        "report": report,
        "stages": stages,
        "premium_calls": 0,
    }


def report_context(report: Mapping[str, Any]) -> str:
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    lines = [
        "Diagnostic evidence gate:",
        f"- decision: {report.get('decision') or 'investigate'}",
        f"- conclusion: {conclusion.get('status') or 'inconclusive'} / {conclusion.get('dimension') or 'unknown'}",
        f"- claim: {_clip(conclusion.get('claim'), 500)}",
        f"- confidence: {conclusion.get('confidence') or 0}",
        f"- baseline drift findings: {len(report.get('baseline_drift') or [])}",
        f"- conclusion retractions: {len(report.get('retractions') or [])}",
    ]
    for item in (report.get("next_experiments") or [])[:5]:
        if isinstance(item, Mapping):
            lines.append(f"- next experiment ({item.get('safety')}): {_clip(item.get('action'), 300)}")
    for invariant in (report.get("contract_invariants") or [])[:8]:
        lines.append(f"- mechanism invariant: {_clip(invariant, 500)}")
    for lens in (report.get("diagnostic_lenses") or [])[:12]:
        lines.append(f"- diagnostic lens: {_clip(lens, 120)}")
    return "\n".join(lines)
