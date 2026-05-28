"""Deterministic pattern parser for simple measurable setup descriptions."""
from __future__ import annotations

import json
import re
from typing import Any

_MECHANICAL_INDICATORS: tuple[tuple[str, str], ...] = (
    (r"\bema[\s_-]?200\b", "ema_200"),
    (r"\bema[\s_-]?100\b", "ema_100"),
    (r"\bema[\s_-]?50\b", "ema_50"),
    (r"\bema[\s_-]?21\b", "ema_21"),
    (r"\bema[\s_-]?20\b", "ema_20"),
    (r"\bema[\s_-]?9\b", "ema_9"),
    (r"\bsma[\s_-]?200\b", "sma_200"),
    (r"\bsma[\s_-]?100\b", "sma_100"),
    (r"\bsma[\s_-]?50\b", "sma_50"),
    (r"\bsma[\s_-]?20\b", "sma_20"),
    (r"\brsi(?:[\s_-]?14)?\b", "rsi_14"),
    (r"\bmacd(?:[\s_-]?hist(?:ogram)?)\b", "macd_hist"),
    (r"\bmacd[\s_-]?hist(?:ogram)?\b", "macd_hist"),
    (r"\badx\b", "adx"),
    (r"\b(?:relative\s+volume|rel[\s_-]?vol|rvol|volume\s+ratio)\b", "rel_vol"),
    (r"\bresistance\s+retests?\b", "resistance_retests"),
    (
        r"\b(?:distance|dist)\s+to\s+resistance(?:\s+pct|\s+percent|%)?\b",
        "dist_to_resistance_pct",
    ),
    (r"\bvcp(?:\s+count)?\b", "vcp_count"),
    (r"\bprice\b", "price"),
    (r"\bvwap\b", "vwap_reclaim"),
)

_MECHANICAL_OPERATOR_PATTERNS: tuple[tuple[str, str], ...] = (
    (r">=", ">="),
    (r"<=", "<="),
    (r"==", "=="),
    (r"!=", "!="),
    (r">", ">"),
    (r"<", "<"),
    (r"=", "=="),
    (r"\bat\s+least\b", ">="),
    (r"\bno\s+less\s+than\b", ">="),
    (r"\bgreater\s+than\s+or\s+equal\s+to\b", ">="),
    (r"\babove\s+or\s+equal\s+to\b", ">="),
    (r"\bover\s+or\s+equal\s+to\b", ">="),
    (r"\bat\s+most\b", "<="),
    (r"\bno\s+more\s+than\b", "<="),
    (r"\bless\s+than\s+or\s+equal\s+to\b", "<="),
    (r"\bbelow\s+or\s+equal\s+to\b", "<="),
    (r"\bgreater\s+than\b", ">"),
    (r"\babove\b", ">"),
    (r"\bover\b", ">"),
    (r"\bless\s+than\b", "<"),
    (r"\bunder\b", "<"),
    (r"\bbelow\b", "<"),
    (r"\bequals?\b", "=="),
    (r"\bis\b", "=="),
)

_NUMBER_RE = r"[-+]?\d+(?:\.\d+)?"


def _mechanical_split_segments(description: str) -> list[str]:
    protected = re.sub(
        rf"(\bbetween\s+{_NUMBER_RE})\s+and\s+({_NUMBER_RE})",
        r"\1 __MECH_AND__ \2",
        description.strip().lower(),
    )
    parts = re.split(r"\s*(?:,|;|\+|\bwith\b|\bplus\b|\band\b)\s*", protected)
    return [
        p.replace("__MECH_AND__", "and").replace("__mech_and__", "and").strip()
        for p in parts
        if p.strip()
    ]


def _mechanical_indicators(segment: str) -> list[str]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pattern, indicator in _MECHANICAL_INDICATORS:
        match = re.search(pattern, segment, flags=re.IGNORECASE)
        if match and indicator not in seen:
            found.append((match.start(), indicator))
            seen.add(indicator)
    return [indicator for _, indicator in sorted(found, key=lambda item: item[0])]


def _mechanical_between_condition(segment: str, indicator: str) -> dict[str, Any] | None:
    match = re.search(
        rf"\bbetween\s+({_NUMBER_RE})\s+(?:and|to|-)\s+({_NUMBER_RE})\b",
        segment,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    lo = float(match.group(1))
    hi = float(match.group(2))
    return {"indicator": indicator, "op": "between", "value": [min(lo, hi), max(lo, hi)]}


def _mechanical_operator(segment: str) -> tuple[str, int] | None:
    matches: list[tuple[int, int, str]] = []
    for pattern, op in _MECHANICAL_OPERATOR_PATTERNS:
        match = re.search(pattern, segment, flags=re.IGNORECASE)
        if match:
            matches.append((match.start(), match.end(), op))
    if not matches:
        return None
    _start, end, op = sorted(matches, key=lambda item: item[0])[0]
    return op, end


def _mechanical_number_after(segment: str, start: int) -> float | None:
    match = re.search(_NUMBER_RE, segment[start:])
    if not match:
        return None
    return float(match.group(0))


def _mechanical_condition_from_segment(segment: str) -> dict[str, Any] | None:
    if not segment:
        return None

    if re.search(r"\b(?:bb|bollinger)\b.*\bsqueeze\b|\bsqueeze\b.*\b(?:bb|bollinger)\b", segment):
        return {"indicator": "bb_squeeze", "op": "==", "value": True}
    if re.search(
        r"\bvwap\b.*\b(?:reclaim|cross|break|above|over)\b|"
        r"\b(?:reclaim|cross|break|above|over)\b.*\bvwap\b",
        segment,
    ):
        return {"indicator": "vwap_reclaim", "op": "==", "value": True}
    nr_match = re.search(r"\bnr\s*([47])\b", segment)
    if nr_match:
        return {"indicator": "narrow_range", "op": "==", "value": f"NR{nr_match.group(1)}"}
    if re.search(r"\bnarrow\s+range\b", segment):
        return {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]}

    indicators = _mechanical_indicators(segment)
    if not indicators:
        return None

    between = _mechanical_between_condition(segment, indicators[0])
    if between:
        return between

    op_match = _mechanical_operator(segment)
    if not op_match:
        return None
    op, value_start = op_match

    if len(indicators) >= 2:
        return {"indicator": indicators[0], "op": op, "ref": indicators[1]}

    value = _mechanical_number_after(segment, value_start)
    if value is None:
        return None
    return {"indicator": indicators[0], "op": op, "value": value}


def _mechanical_pattern_name(conditions: list[dict[str, Any]]) -> str:
    labels: list[str] = []
    for condition in conditions:
        label = str(condition.get("indicator", "pattern")).replace("_", " ").upper()
        if label not in labels:
            labels.append(label)
    return " + ".join(labels[:4]) + " Setup"


def mechanical_pattern_suggestion(description: str) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for segment in _mechanical_split_segments(description):
        condition = _mechanical_condition_from_segment(segment)
        if not condition:
            continue
        signature = json.dumps(condition, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        conditions.append(condition)

    if len(conditions) < 2:
        return None

    return {
        "name": _mechanical_pattern_name(conditions),
        "description": description,
        "conditions": conditions,
        "score_boost": 1.0,
        "min_base_score": 4.0,
        "source": "mechanical",
    }


def mechanical_patterns_from_content(
    content: str,
    existing_names: set[str],
    *,
    max_patterns: int = 3,
) -> list[dict[str, Any]]:
    """Extract explicit, measurable pattern snippets without an LLM call."""
    normalized_existing = {str(name).strip().lower() for name in existing_names or set()}
    pieces = [
        piece.strip()
        for piece in re.split(r"(?<=[.!?])\s+|\n+", content or "")
        if piece.strip()
    ]
    windows: list[str] = []
    for idx, piece in enumerate(pieces):
        windows.append(piece)
        if idx + 1 < len(pieces):
            windows.append(f"{piece} {pieces[idx + 1]}")

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for window in windows:
        if len(window) < 12:
            continue
        suggestion = mechanical_pattern_suggestion(window[:700])
        if not suggestion:
            continue
        name = str(suggestion.get("name") or "").strip()
        if not name or name.lower() in normalized_existing:
            continue
        signature = json.dumps(suggestion.get("conditions", []), sort_keys=True, default=str)
        dedupe_key = f"{name.lower()}:{signature}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        suggestion.setdefault("asset_class", "all")
        found.append(suggestion)
        if len(found) >= max_patterns:
            break
    return found
