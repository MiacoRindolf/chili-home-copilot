"""Deterministic pattern parser for simple measurable setup descriptions."""
from __future__ import annotations

import json
import re
from typing import Any

_MECHANICAL_INDICATORS: tuple[tuple[str, str], ...] = (
    (r"\b200[\s_-]?(?:day\s+)?ema\b", "ema_200"),
    (r"\bema[\s_-]?200\b", "ema_200"),
    (r"\b100[\s_-]?(?:day\s+)?ema\b", "ema_100"),
    (r"\bema[\s_-]?100\b", "ema_100"),
    (r"\b50[\s_-]?(?:day\s+)?ema\b", "ema_50"),
    (r"\bema[\s_-]?50\b", "ema_50"),
    (r"\b21[\s_-]?(?:day\s+)?ema\b", "ema_21"),
    (r"\bema[\s_-]?21\b", "ema_21"),
    (r"\b20[\s_-]?(?:day\s+)?ema\b", "ema_20"),
    (r"\bema[\s_-]?20\b", "ema_20"),
    (r"\b9[\s_-]?(?:day\s+)?ema\b", "ema_9"),
    (r"\bema[\s_-]?9\b", "ema_9"),
    (r"\b200[\s_-]?(?:day\s+)?sma\b", "sma_200"),
    (r"\bsma[\s_-]?200\b", "sma_200"),
    (r"\b100[\s_-]?(?:day\s+)?sma\b", "sma_100"),
    (r"\bsma[\s_-]?100\b", "sma_100"),
    (r"\b50[\s_-]?(?:day\s+)?sma\b", "sma_50"),
    (r"\bsma[\s_-]?50\b", "sma_50"),
    (r"\b20[\s_-]?(?:day\s+)?sma\b", "sma_20"),
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
    (r"\b(?:price|close|last|last\s+price|closing\s+price)\b", "price"),
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
_COUNT_WORDS = {
    "two": 2.0,
    "twice": 2.0,
    "three": 3.0,
    "third": 3.0,
    "thrice": 3.0,
    "four": 4.0,
    "fourth": 4.0,
}
_COUNT_RE = rf"(?:{_NUMBER_RE}|{'|'.join(_COUNT_WORDS)})"
_WEB_SAFE_EMA_SUPPORT_INDICATORS = {"ema_20", "ema_50", "ema_100"}


def _mechanical_count_value(raw: str) -> float:
    normalized = raw.strip().lower()
    if normalized in _COUNT_WORDS:
        return _COUNT_WORDS[normalized]
    return float(normalized)


def _mechanical_split_segments(description: str) -> list[str]:
    protected = re.sub(
        rf"(\bbetween\s+{_NUMBER_RE})\s+and\s+({_NUMBER_RE})",
        r"\1 __MECH_AND__ \2",
        description.strip().lower(),
    )
    protected = re.sub(r"\bvcp\s*(\d+)\s*\+", r"vcp \1 contractions", protected)
    protected = re.sub(
        r"\b(\d+)\s*\+\s+(?=(?:successive\s+)?(?:volatility\s+)?contractions?\b)",
        r"\1 ",
        protected,
    )
    protected = re.sub(r"\bmacd\s*\+", "macd positive", protected)
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


def _mechanical_condition_from_segment(segment: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    if not segment:
        return None

    if re.search(
        r"\b(?:bearish|broken)\s+ema\s+stack(?:ing)?\b|\bema\s+stack(?:ing)?\s+(?:bearish|broken)\b|"
        r"\bfull\s+bearish\s+ema\s+stack\b",
        segment,
    ):
        return [
            {"indicator": "price", "op": "<", "ref": "ema_20"},
            {"indicator": "ema_20", "op": "<", "ref": "ema_50"},
            {"indicator": "ema_50", "op": "<", "ref": "ema_100"},
        ]
    if re.search(
        r"\b(?:bullish\s+)?ema\s+stack(?:ing)?\b|\bema\s+stack(?:ing)?\s+bullish\b|"
        r"\bfull\s+(?:bullish\s+)?ema\s+stack\b",
        segment,
    ):
        return [
            {"indicator": "price", "op": ">", "ref": "ema_20"},
            {"indicator": "ema_20", "op": ">", "ref": "ema_50"},
            {"indicator": "ema_50", "op": ">", "ref": "ema_100"},
        ]
    if re.search(r"\bgolden\s+cross\b", segment):
        return {"indicator": "ema_50", "op": ">", "ref": "ema_200"}
    if re.search(r"\bdeath\s+cross\b", segment):
        return {"indicator": "ema_50", "op": "<", "ref": "ema_200"}
    if re.search(
        r"\bmacd\s*\+\b|"
        r"\bmacd\b.*\b(?:positive|bullish|above\s+zero|over\s+zero|cross(?:es|ing)?\s+bullish)\b|"
        r"\bmacd\s+(?:hist(?:ogram)?\s+)?(?:expanding|rising|improving|turning\s+up)\b|"
        r"\b(?:positive|bullish)\s+macd\b",
        segment,
    ):
        return {"indicator": "macd_hist", "op": ">", "value": 0.0}
    if re.search(
        r"\bmacd\s*-\b|"
        r"\bmacd\b.*\b(?:negative|bearish|below\s+zero|under\s+zero|cross(?:es|ing)?\s+bearish)\b|"
        r"\b(?:negative|bearish)\s+macd\b",
        segment,
    ):
        return {"indicator": "macd_hist", "op": "<", "value": 0.0}
    if re.search(r"\brsi\b.*\bnear[-\s]?oversold\b|\bnear[-\s]?oversold\b.*\brsi\b", segment):
        return {"indicator": "rsi_14", "op": "<", "value": 40.0}
    if re.search(r"\brsi\b.*\b(?:deeply\s+)?oversold\b|\b(?:deeply\s+)?oversold\b.*\brsi\b", segment):
        return {"indicator": "rsi_14", "op": "<", "value": 30.0}
    if re.search(r"\brsi\b.*\boverbought\b|\boverbought\b.*\brsi\b", segment):
        return {"indicator": "rsi_14", "op": ">", "value": 70.0}
    if re.search(r"\brsi\b.*\b(?:not\s+overbought|room\s+to\s+run)\b", segment):
        return {"indicator": "rsi_14", "op": "<=", "value": 70.0}
    if re.search(r"\brsi\b.*\b(?:rising|improving|turning\s+up|strengthening)\b", segment):
        return {"indicator": "rsi_14", "op": ">", "value": 55.0}
    if re.search(r"\brsi\b.*\bneutral\b|\bneutral\b.*\brsi\b", segment):
        return {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]}
    if re.search(
        r"\badx\b.*\b(?:low|weak|quiet|compression|consolidation|range(?:bound)?)\b|"
        r"\b(?:low|weak|quiet|compression|consolidation|range(?:bound)?)\b.*\badx\b",
        segment,
    ):
        return {"indicator": "adx", "op": "<", "value": 20.0}
    if re.search(
        r"\badx\b.*\b(?:strong|elevated|trend(?:ing)?|trend\s+strength|confirmation)\b|"
        r"\b(?:strong|elevated|trend(?:ing)?|trend\s+strength|confirmation)\b.*\badx\b",
        segment,
    ):
        return {"indicator": "adx", "op": ">=", "value": 20.0}
    volume_multiple_match = re.search(
        rf"\b({_NUMBER_RE})\s*x\s+(?:relative\s+)?volume\b|"
        rf"\b(?:relative\s+volume|rel[\s_-]?vol|rvol|volume)\s+"
        rf"(?:at\s+least|over|above|greater\s+than|>=?)\s*({_NUMBER_RE})\s*x?\b",
        segment,
    )
    if volume_multiple_match:
        raw_value = next(group for group in volume_multiple_match.groups() if group is not None)
        return {"indicator": "rel_vol", "op": ">=", "value": float(raw_value)}
    if re.search(
        r"\b(?:volume\s+spike|volume\s+breakout|high\s+volume|"
        r"volume\s+surge|volume\s+burst|unusual\s+volume|"
        r"surging\s+volume|rising\s+volume|increasing\s+volume|"
        r"volume\s+expansion|expanding\s+volume|rvol\s+spike)\b",
        segment,
    ):
        return {"indicator": "rel_vol", "op": ">=", "value": 1.5}
    if re.search(
        r"\b(?:squeeze\s+(?:firing|releasing|release|fires?)|"
        r"(?:volatility|bollinger|bb)\s+squeeze\s+(?:firing|releasing|release|fires?))\b",
        segment,
    ):
        return {"indicator": "bb_squeeze", "op": "==", "value": True}
    if re.search(r"\b(?:bb|bollinger)\b.*\bsqueeze\b|\bsqueeze\b.*\b(?:bb|bollinger)\b", segment):
        return {"indicator": "bb_squeeze", "op": "==", "value": True}
    if re.search(
        r"\bvwap\b.*\b(?:support|hold(?:s|ing)?|held)\b|"
        r"\b(?:support|hold(?:s|ing)?|held)\b.*\bvwap\b",
        segment,
    ):
        return {"indicator": "vwap_reclaim", "op": "==", "value": True}
    if re.search(
        r"\bvwap\b.*\b(?:reclaim|cross|break|above|over)\b|"
        r"\b(?:reclaim|cross|break|above|over)\b.*\bvwap\b",
        segment,
    ):
        return {"indicator": "vwap_reclaim", "op": "==", "value": True}
    if (
        re.search(
            r"\b(?:support|hold(?:s|ing)?|held)\b.*\bema\b|"
            r"\bema\b.*\b(?:support|hold(?:s|ing)?|held)\b",
            segment,
        )
        and not re.search(
            r"\b(?:fail(?:s|ed)?\s+to\s+hold|lost|los(?:e|es|ing)|below|under|"
            r"break(?:s|ing)?\s+below)\b",
            segment,
        )
    ):
        for indicator in _mechanical_indicators(segment):
            if indicator in _WEB_SAFE_EMA_SUPPORT_INDICATORS:
                return {"indicator": "price", "op": ">", "ref": indicator}
    nr_match = re.search(r"\bnr\s*([47])\b", segment)
    if nr_match:
        return {"indicator": "narrow_range", "op": "==", "value": f"NR{nr_match.group(1)}"}
    if re.search(
        r"\bnarrow\s+range\b|\btight(?:est)?\s+range\b|\bcoiling\b|\bcoil(?:ed|s)?\b",
        segment,
    ):
        return {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]}
    resistance_retest_match = re.search(
        rf"\b({_COUNT_RE})\s+resistance\s+(?:retests?|tests?|touches?)\b|"
        rf"\b({_COUNT_RE})\s+tests?\s+of\s+resistance\b|"
        rf"\bresistance\b.*\b(?:retested|tested|touched)\s+({_COUNT_RE})\s+times?\b",
        segment,
    )
    if resistance_retest_match:
        raw_count = next(group for group in resistance_retest_match.groups() if group is not None)
        return {
            "indicator": "resistance_retests",
            "op": ">=",
            "value": _mechanical_count_value(raw_count),
        }
    if re.search(r"\b(?:multiple|several)\s+resistance\s+(?:retests?|tests?|touches?)\b", segment):
        return {"indicator": "resistance_retests", "op": ">=", "value": 2.0}
    resistance_distance_match = re.search(
        rf"\b(?:price|close|last|stock|ticker)?\s*(?:is\s+)?"
        rf"(?:within|inside|no\s+more\s+than|less\s+than|under)\s+({_COUNT_RE})\s*(?:%|percent)?"
        rf"\s+(?:of|below|from)\s+resistance\b",
        segment,
    )
    if resistance_distance_match:
        return {
            "indicator": "dist_to_resistance_pct",
            "op": "<=",
            "value": _mechanical_count_value(resistance_distance_match.group(1)),
        }
    if re.search(r"\b(?:near|close\s+to|just\s+below)\s+resistance\b", segment):
        return {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 2.0}
    contraction_count_match = re.search(
        r"\b(\d+)\s*\+?\s+(?:successive\s+)?(?:volatility\s+)?contractions?\b",
        segment,
    )
    if contraction_count_match:
        return {"indicator": "vcp_count", "op": ">=", "value": float(contraction_count_match.group(1))}
    vcp_match = re.search(
        r"\b(?:vcp|volatility\s+contraction\s+pattern|volume\s+contraction\s+pattern)\b",
        segment,
    )
    if vcp_match:
        count_match = re.search(
            r"\bvcp\s*(\d+)\b|\b(\d+)\s*\+?\s+(?:successive\s+)?contractions?\b",
            segment,
        )
        value = float(next(group for group in count_match.groups() if group is not None)) if count_match else 2.0
        return {"indicator": "vcp_count", "op": ">=", "value": value}

    indicators = _mechanical_indicators(segment)
    if not indicators:
        return None

    between = _mechanical_between_condition(segment, indicators[0])
    if between:
        return between

    if len(indicators) >= 2:
        if re.search(r"\b(?:cross(?:es|ing)?|break(?:s|ing)?|move(?:s|ing)?)\s+above\b|\babove\b|\bover\b", segment):
            return {"indicator": indicators[0], "op": ">", "ref": indicators[1]}
        if re.search(r"\b(?:cross(?:es|ing)?|break(?:s|ing)?|move(?:s|ing)?)\s+below\b|\bbelow\b|\bunder\b", segment):
            return {"indicator": indicators[0], "op": "<", "ref": indicators[1]}

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
        parsed = _mechanical_condition_from_segment(segment)
        if not parsed:
            continue
        segment_conditions = parsed if isinstance(parsed, list) else [parsed]
        for condition in segment_conditions:
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
