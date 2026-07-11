"""Evidence-gated diagnostic reasoning for local Project Autonomy.

The local model supplies semantic hypotheses.  This module owns the parts that
must not depend on model confidence: evidence provenance, independent support,
counter-evidence, baseline drift, safe experiment boundaries, and conclusion
retraction.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    ("data", ("dataset", "row count", "table", "data source", "ingestion source", "sink", "feed", "cache", "snapshot data", "nbbo")),
    ("state", ("state", "queue", "pending", "session", "board", "checkpoint", "stale row", "lifecycle")),
    ("config", ("config", "setting", "flag", "environment variable", " env ", "feature gate")),
    ("dependency", ("dependency", "provider", "service", "socket", "network", "database server", "broker api")),
    ("runtime", ("runtime", "container", "worker", "process", "restart", "image", "deployment")),
    ("test_harness", ("replay", "harness", "fixture", "mock", "test database", "simulator")),
    ("code", ("code", "commit", "revision", "diff", "function", "caller", "branch", "patch", "source edit", "source inspection")),
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
        ),
    ),
    (
        "runtime",
        (
            ("recreating only", 6),
            ("running worker image", 6),
            ("image label", 5),
            ("loaded module hash", 5),
            ("pre-fix behavior", 3),
        ),
    ),
    (
        "test_harness",
        (
            ("serialized replay input", 4),
            ("replay fixture", 3),
            ("focused test", 2),
        ),
    ),
    (
        "code",
        (
            ("source diff", 5),
            ("source inspection", 4),
            ("additional source edit", 2),
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


def infer_dimension(statement: str) -> str:
    lower = f" {statement.lower()} "
    scores: dict[str, int] = {}
    for dimension, terms in _DIMENSION_TERMS:
        scores[dimension] = sum(1 for term in terms if term in lower)
    for dimension, weighted_phrases in _DIMENSION_PHRASE_WEIGHTS:
        scores[dimension] = scores.get(dimension, 0) + sum(
            weight for phrase, weight in weighted_phrases if phrase in lower
        )
    best = max(scores, key=scores.get, default="unknown")
    return best if scores.get(best, 0) else "unknown"


def derive_contract_invariants(statement: str) -> list[str]:
    """Extract reusable mechanism contracts without asking the local model."""
    lowered = str(statement or "").lower()
    invariants: list[str] = []
    if (
        any(token in lowered for token in ("single-flight", "singleflight", "in-flight", "poison"))
        and any(token in lowered for token in ("retry", "later", "same key", "start fresh"))
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
    return invariants[:8]


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
    if any("in-flight work must be evicted" in value for value in invariants):
        owners = [
            content
            for content in lowered_files.values()
            if "new map" in content and any(token in content for token in ("pending", "flight"))
        ]
        if owners and not any(_singleflight_owner_evicts_key(content) for content in owners):
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
    return warnings


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


def _replace_braced_dart_callable_body(content: str, name: str, body: str) -> str | None:
    match = re.search(
        rf"((?:Future(?:<[^>]+>)?|void|[A-Za-z_]\w*(?:<[^>{{}}]+>)?\??)\s+"
        rf"{re.escape(name)}(?:<[^>{{}}]+>)?\s*\((.*?)\)\s*(?:async\s*)?)\{{",
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


def contract_repair_proposals(
    prompt: str,
    files: Mapping[str, str],
) -> dict[str, str]:
    """Synthesize narrow repairs only when a known invariant is violated."""
    warnings = contract_invariant_warnings(prompt, files)
    if not warnings:
        return {}
    invariants = derive_contract_invariants(prompt)
    proposals: dict[str, str] = {}
    if any("in-flight work must be evicted" in value for value in invariants):
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
    return proposals


def looks_like_diagnostic_request(prompt: str) -> bool:
    lower = str(prompt or "").lower()
    return any(marker in lower for marker in _DIAGNOSTIC_MARKERS)


def derive_diagnostic_lenses(statement: str) -> list[str]:
    """Select generic deep-diagnosis lenses without asserting any root cause."""
    lower = str(statement or "").lower()
    lenses = list(_BASE_DIAGNOSTIC_LENSES)
    for lens, markers in _DIAGNOSTIC_LENS_RULES:
        if any(marker in lower for marker in markers):
            lenses.append(lens)
    return list(dict.fromkeys(lenses))[:12]


def normalize_evidence(raw: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    statement = _clip(raw.get("statement"), 900)
    explicit_dimension = str(raw.get("dimension") or "").strip().lower()
    dimension = (
        explicit_dimension
        if explicit_dimension in DIMENSIONS and explicit_dimension != "unknown"
        else infer_dimension(statement)
    )
    kind = str(raw.get("kind") or "observation").strip().lower()
    if kind not in {"observation", "experiment", "artifact", "metric"}:
        kind = "observation"
    return {
        "evidence_id": _clean_id(raw.get("evidence_id"), f"evidence-{index + 1}"),
        "statement": statement,
        "dimension": dimension,
        "kind": kind,
        "provenance": _clip(raw.get("provenance") or f"unattributed:{index + 1}", 300),
        "independence_key": _clip(raw.get("independence_key") or raw.get("provenance") or f"source:{index + 1}", 200),
        "reliability": _clamp_reliability(raw.get("reliability")),
        "discriminating": bool(raw.get("discriminating")),
        "comparison_key": _clip(raw.get("comparison_key"), 160),
        "code_revision": _clip(raw.get("code_revision"), 100),
        "input_fingerprint": _clip(raw.get("input_fingerprint"), 160),
        "environment_fingerprint": _clip(raw.get("environment_fingerprint"), 160),
        "outcome_fingerprint": _clip(raw.get("outcome_fingerprint"), 200),
        "experiment_id": _clean_id(raw.get("experiment_id"), "") if raw.get("experiment_id") else "",
    }


def normalize_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    observations = [
        normalize_evidence(item, index)
        for index, item in enumerate(raw.get("observations") or [])
        if isinstance(item, Mapping)
    ][:40]
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


def _normalize_hypothesis(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    dimension = str(raw.get("dimension") or "unknown").strip().lower()
    if dimension not in DIMENSIONS:
        dimension = infer_dimension(str(raw.get("claim") or ""))
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


def _experiment_result_ids(packet: Mapping[str, Any]) -> set[str]:
    return {
        str(evidence_id)
        for experiment in packet.get("experiments") or []
        if isinstance(experiment, Mapping) and experiment.get("status") == "completed"
        for evidence_id in experiment.get("result_evidence_ids") or []
    }


def _typed_probe_fallback_hypotheses(
    case: Mapping[str, Any],
    hypotheses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recover strong causal families omitted by a small local model.

    The fallback is deliberately narrow: only high-reliability, discriminating
    evidence emitted by CHILI's typed probes can create a hypothesis. Ordinary
    prompt or repository evidence still requires the model to propose one.
    """
    existing_dimensions = {
        str(item.get("dimension") or "unknown")
        for item in hypotheses
        if isinstance(item, Mapping)
    }
    existing_ids = {
        str(item.get("hypothesis_id") or "")
        for item in hypotheses
        if isinstance(item, Mapping)
    }
    evidence_by_dimension: dict[str, list[str]] = defaultdict(list)
    for record in case.get("observations") or []:
        if not isinstance(record, Mapping):
            continue
        dimension = str(record.get("dimension") or "unknown")
        provenance = str(record.get("provenance") or "")
        if (
            dimension not in DIMENSIONS
            or dimension == "unknown"
            or dimension in existing_dimensions
            or not provenance.startswith("diagnostic_probe:")
            or not bool(record.get("discriminating"))
            or _clamp_reliability(record.get("reliability")) < 0.9
        ):
            continue
        evidence_id = str(record.get("evidence_id") or "")
        if evidence_id and evidence_id not in evidence_by_dimension[dimension]:
            evidence_by_dimension[dimension].append(evidence_id)

    fallbacks: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        evidence_ids = evidence_by_dimension.get(dimension) or []
        if not evidence_ids:
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
                "claim": f"Typed diagnostic evidence identifies {label} as the primary causal dimension.",
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
            and str(evidence_by_id[str(value)].get("dimension") or "unknown")
            not in {hypothesis_dimension, "unknown"}
        )
        if mismatched_support:
            errors.append(
                f"{hypothesis_id} links support from a different evidence family: "
                + ", ".join(mismatched_support)
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
    completed_result_ids = _experiment_result_ids(packet)
    drift = detect_baseline_drift(case["observations"])

    hypotheses = [
        *packet["hypotheses"],
        *_typed_probe_fallback_hypotheses(case, packet["hypotheses"]),
    ]
    hypothesis_results: list[dict[str, Any]] = []
    for item in hypotheses:
        support_ids = list(dict.fromkeys(item.get("support_evidence_ids") or []))
        contradict_ids = list(dict.fromkeys(item.get("contradict_evidence_ids") or []))
        if not support_ids:
            support_ids = [
                evidence_id
                for evidence_id, record in evidence.items()
                if record.get("dimension") == item.get("dimension")
            ]
        support_records = [evidence[value] for value in support_ids if value in evidence]
        contradict_records = [evidence[value] for value in contradict_ids if value in evidence]
        support_weight = _independent_weight(support_records)
        confirmatory_weight = _confirmatory_weight(support_records)
        contradict_weight = _independent_weight(contradict_records)
        discriminating = any(
            bool(record.get("discriminating")) or str(record.get("evidence_id")) in completed_result_ids
            for record in support_records
        )
        direct_artifact = any(
            record.get("kind") == "artifact" and float(record.get("reliability") or 0) >= 0.9
            for record in support_records
        )
        if contradict_weight >= 0.7:
            status = "refuted"
        elif (
            confirmatory_weight >= 1.25 and (discriminating or direct_artifact)
        ) or (
            confirmatory_weight >= 0.85 and discriminating and direct_artifact
        ):
            status = "supported"
        elif support_weight > 0:
            status = "provisional"
        else:
            status = "untested"
        blockers: list[str] = []
        if item.get("dimension") == "unknown" and status == "supported":
            status = "provisional"
            blockers.append("Unknown is not a confirmable causal family; isolate a known dimension first.")
        if drift and item.get("dimension") == "code" and status in {"supported", "provisional"}:
            status = "blocked"
            blockers.append("Same code and input produced different outcomes; code causality is not isolated.")
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
                "confirmatory_weight": confirmatory_weight,
                "contradict_weight": contradict_weight,
                "discriminating_evidence": discriminating,
                "blockers": blockers,
            }
        )

    results_by_id = {str(item.get("hypothesis_id")): item for item in hypothesis_results}
    requested = packet["conclusion"]
    conclusion_id = str(requested.get("hypothesis_id") or "")
    chosen = results_by_id.get(conclusion_id)
    requested_choice_id = conclusion_id
    if chosen is None and hypothesis_results:
        chosen = max(
            hypothesis_results,
            key=lambda item: (
                item.get("status") == "supported",
                float(item.get("support_weight") or 0) - float(item.get("contradict_weight") or 0),
            ),
        )
        conclusion_id = str(chosen.get("hypothesis_id") or "")
    if chosen is not None and chosen.get("dimension") == "unknown":
        known_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") != "unknown"
            and item.get("status") not in {"refuted", "untested"}
        ]
        if known_candidates:
            chosen = max(
                known_candidates,
                key=lambda item: (
                    item.get("status") == "supported",
                    bool(item.get("discriminating_evidence")),
                    float(item.get("confirmatory_weight") or 0),
                    float(item.get("support_weight") or 0),
                ),
            )
            conclusion_id = str(chosen.get("hypothesis_id") or "")
    if chosen is not None:
        supported = [item for item in hypothesis_results if item.get("status") == "supported"]
        if supported:
            def support_rank(item: Mapping[str, Any]) -> tuple[bool, float, float]:
                return (
                    bool(item.get("discriminating_evidence")),
                    float(item.get("confirmatory_weight") or 0),
                    float(item.get("support_weight") or 0),
                )

            strongest_supported = max(supported, key=support_rank)
            if chosen.get("status") != "supported" or support_rank(strongest_supported) > support_rank(chosen):
                chosen = strongest_supported
                conclusion_id = str(chosen.get("hypothesis_id") or "")

    problem_dimension = infer_dimension(str(case.get("problem_statement") or ""))
    if (
        chosen is not None
        and problem_dimension not in {"unknown", str(chosen.get("dimension") or "unknown")}
        and not bool(chosen.get("discriminating_evidence"))
    ):
        problem_candidates = [
            item
            for item in hypothesis_results
            if item.get("dimension") == problem_dimension
            and item.get("status") not in {"refuted", "untested", "blocked"}
        ]
        if problem_candidates:
            problem_candidate = max(
                problem_candidates,
                key=lambda item: (
                    float(item.get("support_weight") or 0),
                    float(item.get("confirmatory_weight") or 0),
                ),
            )
            if (
                float(problem_candidate.get("support_weight") or 0) + 0.9
                > float(chosen.get("support_weight") or 0)
            ):
                chosen = problem_candidate
                conclusion_id = str(chosen.get("hypothesis_id") or "")

    requested_status = str(requested.get("status") or "provisional")
    if chosen is None:
        effective_status = "inconclusive"
    elif requested_status == "rejected" or chosen.get("status") == "refuted":
        effective_status = "rejected"
    elif (
        requested_status not in {"inconclusive", "rejected"}
        and chosen.get("status") == "supported"
        and not errors
    ):
        effective_status = "confirmed"
    elif chosen.get("status") in {"blocked", "untested"}:
        effective_status = "inconclusive"
    else:
        effective_status = "provisional"

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

    if effective_status == "confirmed":
        decision = "patch_root_cause"
    elif drift or any(item.get("status") in {"provisional", "blocked"} for item in hypothesis_results):
        decision = "instrument_first"
    else:
        decision = "investigate"

    recommendations = recommend_counterfactuals(case, packet, hypothesis_results, drift)
    selected_evidence_ids = list(requested.get("evidence_ids") or [])
    selected_reason = str(requested.get("reason") or "")
    if conclusion_id and conclusion_id != requested_choice_id and chosen is not None:
        selected_evidence_ids = list(chosen.get("support_evidence_ids") or [])
        selected_reason = (
            "Deterministic evidence gate selected a stronger supported competing hypothesis."
        )
    return {
        "schema": REPORT_SCHEMA,
        "case_id": case["case_id"],
        "valid": not errors,
        "errors": errors,
        "baseline_drift": drift,
        "hypothesis_results": hypothesis_results,
        "conclusion": {
            "hypothesis_id": conclusion_id,
            "status": effective_status,
            "dimension": str(chosen.get("dimension") or "unknown") if chosen else "unknown",
            "claim": str(chosen.get("claim") or "") if chosen else "",
            "confidence": float(chosen.get("confidence") or 0) if chosen else 0.0,
            "evidence_ids": selected_evidence_ids,
            "reason": selected_reason,
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
            -sum(float(item.get("reliability") or 0) for item in pair[1]),
            pair[0] == "unknown",
            pair[0],
        ),
    )
    hypotheses: list[dict[str, Any]] = []
    experiments: list[dict[str, Any]] = []
    for index, (dimension, records) in enumerate(ranked[:4]):
        hypothesis_id = f"h-{dimension}"
        hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "claim": f"The observed failure is primarily caused by {dimension} drift.",
                "dimension": dimension,
                "support_evidence_ids": [str(item.get("evidence_id")) for item in records],
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


def _case_prompt(case: Mapping[str, Any]) -> str:
    safe_case = {
        "case_id": case.get("case_id"),
        "problem_statement": case.get("problem_statement"),
        "observations": case.get("observations"),
        "prior_conclusion": case.get("prior_conclusion"),
        "constraints": case.get("constraints"),
    }
    return json.dumps(safe_case, indent=2, sort_keys=True)


def _packet_shape() -> str:
    return (
        '{"schema":"chili.diagnostic-packet.v1","problem_statement":"...",'
        '"hypotheses":[{"hypothesis_id":"h1","claim":"...","dimension":"code|data|clock|state|config|dependency|runtime|test_harness|unknown",'
        '"support_evidence_ids":["e1"],"contradict_evidence_ids":[],"falsification":"..."}],'
        '"experiments":[{"experiment_id":"x1","hypothesis_ids":["h1"],"changed_dimensions":["code"],'
        '"held_constant_dimensions":["data"],"expected_if_true":"...","expected_if_false":"...",'
        '"evidence_required":["..."],"result_evidence_ids":[],"safety":"read_only|isolated|runtime|live",'
        '"status":"planned|completed|blocked","auto_execute":false,"probe":{}}],'
        '"conclusion":{"hypothesis_id":"h1","status":"confirmed|provisional|inconclusive|rejected",'
        '"evidence_ids":["e1"],"reason":"..."}}'
    )


def _typed_probe_examples() -> str:
    return (
        'Examples (choose one exact kind, never copy alternatives): '
        '{"probe_id":"p-log","kind":"log_search","paths":["logs"],"query":"connection refused",'
        '"tail_lines":5000,"dimension":"runtime"}; '
        '{"probe_id":"p-schema","kind":"db_schema","table":"trading_events","dimension":"data"}; '
        '{"probe_id":"p-count","kind":"db_profile","table":"trading_events",'
        '"timestamp_column":"created_at","lookback_minutes":60,"group_by":"reason",'
        '"max_groups":15,"filters":{"status":"pending"},"dimension":"data"}.'
    )


def investigator_prompt(raw_case: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the investigator in a local-only diagnostic team. Return JSON only. "
        "Generate competing hypotheses across different dimensions. Link every claim to supplied evidence ids. "
        "Reconstruct expected behavior, observed behavior, and the causal timeline before selecting a root cause. "
        "Separate the earliest causal break from downstream symptoms. Treat every diagnostic lens in the case "
        "as a question to test, not as an assumed answer. For trading or operational incidents, distinguish the "
        "strategy/requirements contract, external conditions, broker or persisted state, evidence-pipeline "
        "coverage, and source-versus-running-revision parity. "
        "A hypothesis without a falsification experiment is invalid. Same code and input with different outcomes "
        "means baseline drift, not proof of a code regression. Never request automatic runtime or live mutation. "
        "When evidence is insufficient, you may set auto_execute=true only for a typed probe from the supplied "
        "catalog. Raw shell commands do not exist. search is fixed-string; targeted_test must name one selector "
        "under tests/. log_search is fixed-string over bounded log tails. db_schema and db_profile never accept "
        "SQL; db_profile exposes only count/group/min/max/avg/sum aggregates and production use requires a "
        "timestamp column plus bounded lookback. Use read_only for repo_state/search/file_excerpt/git_history/"
        "git_diff/log_inventory/log_search/db_schema/db_profile and isolated for compile/targeted_test.\n\n"
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
        "Retract a conclusion rather than defending it when the evidence changed. Never request automatic runtime or live mutation.\n\n"
        f"Required shape:\n{_packet_shape()}\n{_typed_probe_examples()}\n\nCase:\n{_case_prompt(case)}\n\n"
        f"Investigator packet:\n{json.dumps(packet, indent=2, sort_keys=True)}\n\n"
        f"Deterministic evaluation:\n{json.dumps(report, indent=2, sort_keys=True)}"
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
        "db_schema/db_profile. Database probes never accept SQL or raw-row selection. Raw commands are forbidden.\n\n"
        f"Required shape:\n{_packet_shape()}\n{_typed_probe_examples()}\n\nCase:\n{_case_prompt(case)}\n\n"
        f"Challenged packet:\n{json.dumps(packet, indent=2, sort_keys=True)}\n\n"
        f"Deterministic evaluation:\n{json.dumps(report, indent=2, sort_keys=True)}"
    )


ModelCall = Callable[[str, str], str]


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
        parsed = parse_json_object(response)
        accepted = parsed is not None
        candidate = normalize_packet(parsed) if parsed is not None else packet
        next_report = evaluate_packet(case, candidate, previous_report=report)
        candidate_errors = list(next_report.get("errors") or [])
        if parsed is None and response:
            candidate_errors.insert(0, "Model response was not a usable diagnostic JSON object.")
        if not next_report["valid"] and accepted:
            accepted = False
            candidate = packet
            next_report = evaluate_packet(case, candidate, previous_report=report)
        stages.append(
            {
                "stage": stage,
                "accepted": accepted,
                "response_chars": len(response),
                "errors": candidate_errors,
                "conclusion": next_report.get("conclusion") or {},
                "retractions": next_report.get("retractions") or [],
            }
        )
        all_retractions.extend(next_report.get("retractions") or [])
        packet = candidate
        report = next_report

    report = {**report, "retractions": all_retractions}
    return {
        "schema": DEBATE_SCHEMA,
        "case_id": case["case_id"],
        "packet": packet,
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
