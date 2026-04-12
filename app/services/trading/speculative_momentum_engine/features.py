"""Normalize ScanResult into engine inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ....models.trading import ScanResult


@dataclass(frozen=True)
class SignalFeatures:
    ticker: str
    scanner_score: float
    signal: str
    risk_level: str
    blob: str
    vol_ratio: float | None


_SQUEEZE_RE = re.compile(
    r"\b(squeeze|short\s*squeeze|gamma\s*squeeze|ssr|halt|resume|circuit|halted)\b",
    re.I,
)
_EVENT_RE = re.compile(
    r"\b(news|catalyst|fda|earnings|guidance|pr\s|sec\s|filing|contract|partnership)\b",
    re.I,
)
_EXTENSION_RE = re.compile(
    r"\b(extended|extension|parabolic|blow[- ]?off|overbought\s+stretch|too\s+far)\b",
    re.I,
)
_VOLUME_RE = re.compile(
    r"\b(abnormal\s+volume|volume\s+spike|relative\s+volume|rvol|unusual\s+activity)\b",
    re.I,
)
_EXHAUSTION_RE = re.compile(
    r"\b(exhaust|exhaustion|failed\s+continuation|rejection|fade)\b",
    re.I,
)
_VWAP_PULLBACK_RE = re.compile(r"\b(first\s+pullback|pullback|reclaim|vwap)\b", re.I)


def text_blob(sr: ScanResult) -> str:
    parts = [sr.rationale or "", sr.signal or "", str(sr.score or "")]
    ind = sr.indicator_data
    if isinstance(ind, dict):
        for k in ("note", "summary", "scanner_reason", "headline"):
            v = ind.get(k)
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts)


def indicator_volume_hint(ind: dict[str, Any] | None) -> float | None:
    if not isinstance(ind, dict):
        return None
    for key in ("volume_ratio", "relative_volume", "rvol", "vol_ratio"):
        v = ind.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def build_features(sr: ScanResult) -> SignalFeatures:
    t = (sr.ticker or "").strip().upper()
    return SignalFeatures(
        ticker=t,
        scanner_score=float(sr.score or 0),
        signal=(sr.signal or "").strip().lower(),
        risk_level=(sr.risk_level or "medium").strip().lower(),
        blob=text_blob(sr),
        vol_ratio=indicator_volume_hint(sr.indicator_data if isinstance(sr.indicator_data, dict) else None),
    )


_BUY_SIGNALS = frozenset({"buy", "long", "momentum", ""})


def passes_hot_gate(f: SignalFeatures, *, min_score: float = 6.0) -> bool:
    if f.scanner_score < min_score:
        return False
    # Engine is buy-oriented; filter out explicit sell/short signals.
    if f.signal and f.signal not in _BUY_SIGNALS:
        return False
    return (
        f.scanner_score >= 7.8
        or bool(_SQUEEZE_RE.search(f.blob))
        or bool(_VOLUME_RE.search(f.blob))
        or bool(_EXTENSION_RE.search(f.blob))
        or bool(_EVENT_RE.search(f.blob))
        or bool(_VWAP_PULLBACK_RE.search(f.blob))
        or (f.vol_ratio is not None and f.vol_ratio >= 2.5)
    )


# Export regex helpers for nodes module
def squeeze_re() -> re.Pattern[str]:
    return _SQUEEZE_RE


def event_re() -> re.Pattern[str]:
    return _EVENT_RE


def extension_re() -> re.Pattern[str]:
    return _EXTENSION_RE


def volume_re() -> re.Pattern[str]:
    return _VOLUME_RE


def exhaustion_re() -> re.Pattern[str]:
    return _EXHAUSTION_RE


def vwap_pullback_re() -> re.Pattern[str]:
    return _VWAP_PULLBACK_RE
