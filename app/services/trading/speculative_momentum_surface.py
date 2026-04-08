"""Speculative / explosive intraday mover surface (separate from core repeatable-edge engine).

This module implements a **distinct data plane** for names that may behave like parabolic
intraday movers (squeeze, halt/resume context, abnormal momentum language in scanner text, etc.).

Methodology:
- **Best-effort heuristics** over recent ``ScanResult`` rows (AI scanner), not learned promotion.
- Does **not** participate in pattern imminent Tier A/B/C math and must not be merged into
  core promotion or learning hooks without an explicit future phase.

Structured outputs are suitable for an "Explosive movers / speculative momentum" desk panel
and for honest "why not promoted to core edge" copy in the UI.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ...models.trading import ScanResult
from .market_data import is_crypto

logger = logging.getLogger(__name__)

ENGINE_SPECULATIVE_MOMENTUM = "speculative_momentum"
ENGINE_CORE_REPEATABLE_EDGE = "core_repeatable_edge"

# Lexical hints (case-insensitive). Extend deliberately; keep comments honest about inference.
_SQUEEZE_RE = re.compile(
    r"\b(squeeze|short\s*squeeze|gamma\s*squeeze|ssr|halt|resume|circuit|halted)\b",
    re.I,
)
_EVENT_RE = re.compile(
    r"\b(news|catalyst|fda|earnings|guidance|pr\s|sec\s|filing|contract|partnership)\b",
    re.I,
)
_EXTENSION_RE = re.compile(
    r"\b(extended|extension|parabolic|blow[- ]?off|exhaust|overbought\s+stretch|too\s+far)\b",
    re.I,
)
_VOLUME_RE = re.compile(
    r"\b(abnormal\s+volume|volume\s+spike|relative\s+volume|rvol|unusual\s+activity)\b",
    re.I,
)


def _text_blob(sr: ScanResult) -> str:
    parts = [sr.rationale or "", sr.signal or "", str(sr.score or "")]
    ind = sr.indicator_data
    if isinstance(ind, dict):
        for k in ("note", "summary", "scanner_reason", "headline"):
            v = ind.get(k)
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts)


def _indicator_volume_hint(ind: dict[str, Any] | None) -> float | None:
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


def _classify_move_label(blob: str, score: float, vol_ratio: float | None) -> str:
    if _EXTENSION_RE.search(blob):
        return "Blow-Off Risk" if score >= 8.0 else "Too Extended"
    if _SQUEEZE_RE.search(blob):
        return "Speculative Squeeze"
    if _EVENT_RE.search(blob):
        return "Event-Driven Spike"
    if vol_ratio is not None and vol_ratio >= 3.0:
        return "Abnormal Volume Expansion"
    if score >= 7.5:
        return "Momentum Surge (scanner)"
    return "Speculative Momentum"


def _build_scores(
    *,
    score: float,
    blob: str,
    risk_level: str,
    vol_ratio: float | None,
) -> dict[str, Any]:
    """Separate dimensions for UI + future learning (not trained weights yet)."""
    base_momo = min(1.0, max(0.0, score / 10.0))
    lexical_boost = 0.0
    if _SQUEEZE_RE.search(blob):
        lexical_boost += 0.12
    if _VOLUME_RE.search(blob) or (vol_ratio is not None and vol_ratio >= 2.5):
        lexical_boost += 0.1
    if _EVENT_RE.search(blob):
        lexical_boost += 0.08
    speculative_momentum_score = round(min(1.0, base_momo * 0.55 + lexical_boost), 4)

    ext = 0.25
    if _EXTENSION_RE.search(blob):
        ext += 0.45
    if score >= 8.5:
        ext += 0.15
    extension_risk = round(min(1.0, ext), 4)

    exec_r = 0.2
    if (risk_level or "").lower() == "high":
        exec_r += 0.35
    if vol_ratio is not None and vol_ratio >= 4.0:
        exec_r += 0.2
    execution_risk = round(min(1.0, exec_r), 4)

    struct = 0.35
    if vol_ratio is not None and vol_ratio >= 1.5:
        struct += 0.15
    if _VOLUME_RE.search(blob):
        struct += 0.1
    structural_confirmation = round(min(1.0, struct), 4)

    spread_liquidity_quality = None  # No reliable field on ScanResult yet; honest null.

    repeatability_confidence = round(0.22 + 0.08 * min(1.0, score / 10.0), 4)

    core_edge_score = round(max(0.0, 1.0 - speculative_momentum_score) * (structural_confirmation), 4)

    return {
        "speculative_momentum_score": speculative_momentum_score,
        "core_edge_score": core_edge_score,
        "extension_risk": extension_risk,
        "execution_risk": execution_risk,
        "repeatability_confidence": repeatability_confidence,
        "structural_confirmation": structural_confirmation,
        "spread_liquidity_quality": spread_liquidity_quality,
    }


def _why_not_promoted(
    *,
    scores: dict[str, Any],
    move_label: str,
) -> tuple[list[str], list[str]]:
    """Return machine codes and human lines."""
    codes: list[str] = []
    lines: list[str] = []
    lines.append(
        "Core Tier A/B requires a promoted/live ScanPattern evaluated through the imminent "
        "repeatable-edge engine — this row is scanner/pattern-context only."
    )
    codes.append("not_pattern_imminent_engine")

    if scores.get("repeatability_confidence", 1) < 0.45:
        codes.append("low_repeatability_signature")
        lines.append(
            "Low repeatability confidence: explosive scanner narratives rarely match the core "
            "backtested pattern library."
        )
    if scores.get("extension_risk", 0) >= 0.55:
        codes.append("excessive_extension_or_blowoff_language")
        lines.append(
            "Extension / blow-off language or score profile suggests late-stage chase risk "
            "relative to the core entry model."
        )
    if scores.get("execution_risk", 0) >= 0.45:
        codes.append("execution_slippage_risk")
        lines.append(
            "Execution risk is elevated (risk flag and/or thin-liquidity-style volume spike) — "
            "stops may not fill where modeled."
        )
    if "Event" in move_label or "event" in move_label.lower():
        codes.append("event_or_flow_driven")
        lines.append("Event/flow-driven impulse — outside the core structural repeatability thesis.")

    return codes, lines


def build_speculative_momentum_slice(
    db: Session,
    *,
    limit: int = 12,
    min_scanner_score: float = 6.0,
) -> dict[str, Any]:
    """Return a JSON-serializable envelope for the Trading desk (GET opportunity-board)."""
    generated_at = datetime.now(timezone.utc).isoformat()
    items: list[dict[str, Any]] = []
    try:
        rows = (
            db.query(ScanResult)
            .order_by(desc(ScanResult.scanned_at))
            .limit(max(80, limit * 6))
            .all()
        )
    except Exception as e:
        logger.warning("[speculative_momentum] scan query failed: %s", e)
        return {
            "ok": False,
            "engine": ENGINE_SPECULATIVE_MOMENTUM,
            "methodology": "heuristic_scan_result_inference",
            "methodology_note": (
                "Best-effort classification from recent AI scanner rows — not a promoted "
                "pattern signal and not merged into core learning."
            ),
            "generated_at": generated_at,
            "items": [],
            "error": str(e),
        }

    seen: set[str] = set()
    for sr in rows:
        if len(items) >= max(1, limit):
            break
        t = (sr.ticker or "").strip().upper()
        if not t or t in seen:
            continue
        score = float(sr.score or 0)
        if score < min_scanner_score:
            continue
        blob = _text_blob(sr)
        # Gate: must look "hot" — lexical hints or very high score
        vol_ratio = _indicator_volume_hint(sr.indicator_data if isinstance(sr.indicator_data, dict) else None)
        hot = (
            score >= 7.8
            or _SQUEEZE_RE.search(blob)
            or _VOLUME_RE.search(blob)
            or _EXTENSION_RE.search(blob)
            or _EVENT_RE.search(blob)
            or (vol_ratio is not None and vol_ratio >= 2.5)
        )
        if not hot:
            continue

        seen.add(t)
        move_label = _classify_move_label(blob, score, vol_ratio)
        scores = _build_scores(
            score=score,
            blob=blob,
            risk_level=sr.risk_level or "medium",
            vol_ratio=vol_ratio,
        )
        codes, why_lines = _why_not_promoted(scores=scores, move_label=move_label)

        scanned_iso = None
        if sr.scanned_at:
            dt = sr.scanned_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            scanned_iso = dt.isoformat()

        pullback_hint = bool(re.search(r"\b(first\s+pullback|pullback|reclaim|vwap)\b", blob, re.I))

        items.append(
            {
                "ticker": t,
                "asset_class": "crypto" if is_crypto(t) else "stocks",
                "engine": ENGINE_SPECULATIVE_MOMENTUM,
                "move_type_label": move_label,
                "operator_hint": (
                    "First pullback candidate"
                    if pullback_hint and scores["extension_risk"] < 0.65
                    else "Watch only — avoid blind chase"
                    if scores["extension_risk"] >= 0.55
                    else "Speculative — verify liquidity/spread live"
                ),
                "why_interesting": (
                    f"Scanner flagged {sr.signal or 'n/a'} with confluence {score:.1f}/10"
                    + (f" (scanned {scanned_iso})" if scanned_iso else "")
                    + "."
                ),
                "why_speculative": (
                    "Derived from AI scanner text/score — not cross-checked against promoted "
                    "pattern imminent rules or OOS evidence."
                ),
                "why_not_core_promoted_codes": codes,
                "why_not_core_promoted": why_lines,
                "scores": scores,
                "scanner_signal": sr.signal,
                "scanner_score": score,
                "scanner_risk_level": sr.risk_level,
                "scanned_at_utc": scanned_iso,
            }
        )

    return {
        "ok": True,
        "engine": ENGINE_SPECULATIVE_MOMENTUM,
        "methodology": "heuristic_scan_result_inference",
        "methodology_note": (
            "Best-effort classification from recent AI scanner rows. This path is isolated from "
            "core repeatable-edge promotion and learning updates."
        ),
        "generated_at": generated_at,
        "items": items,
    }
