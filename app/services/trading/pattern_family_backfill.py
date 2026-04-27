"""Backfill scan_patterns.hypothesis_family for rows where it's NULL.

Why: KPI strip showed pnl_herfindahl = 0.92 with the "unknown" family
contributing -$1721 vs the only tagged family at +$79. The
concentration warning was misleading — it wasn't a real concentration
risk, just a tagging gap. Untagged patterns roll up to "unknown" in
the diversity rollup, so 4 NULL-family active patterns dominated the
Herfindahl.

Per Hard Rule 3 (CLAUDE.md): "Data-first, code-second. When symptoms
look like wrong FKs / contaminated linkage, fix the DB + add a
migration. Do not paper over it with a router/service filter."

This module fixes the data. Two passes:

  1. Inheritance pass: any pattern with parent_id walks up the chain
     and inherits the first non-NULL ancestor's family. Matches the
     existing `learning.py` variant-creation logic that should have
     done this at insert time but missed when the parent itself was
     NULL (root patterns are processed in pass 2, so the second run
     will fill these in).

  2. Name-keyword pass: for roots (no parent or parent is NULL too),
     classify based on the pattern name + description text. Priority
     order matters because some patterns match multiple keywords
     (e.g., rsi_bullish_divergence_reversal_breakout matches
     divergence, reversal, AND breakout — divergence wins because
     it's the most specific signal).

Idempotent — re-running on a fully tagged DB is a no-op. Designed to
be run from a smoke script with dry_run=True first; flip to False
when the proposed assignments look right.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Priority-ordered list of (keyword, family). First match wins.
# Specific signals (divergence, squeeze) come before broad ones
# (breakout, reversal) so the more informative tag is selected.
_NAME_RULES: list[tuple[str, str]] = [
    # Mean-reversion family — anything driven by an oscillator extreme
    ("divergence", "mean_reversion"),
    ("rsi_oversold", "mean_reversion"),
    ("rsi_overbought", "mean_reversion"),
    ("vwap_revert", "mean_reversion"),
    ("vwap_reclaim", "mean_reversion"),
    ("oversold", "mean_reversion"),
    ("overbought", "mean_reversion"),
    # Compression / continuation — wedge / triangle / flag / squeeze
    ("squeeze", "compression_expansion"),
    ("compression", "compression_expansion"),
    ("ascending triangle", "compression_expansion"),
    ("descending triangle", "compression_expansion"),
    ("symmetric triangle", "compression_expansion"),
    ("triangle", "compression_expansion"),
    ("bull flag", "compression_expansion"),
    ("bear flag", "compression_expansion"),
    ("pennant", "compression_expansion"),
    ("flag", "compression_expansion"),
    ("wedge", "compression_expansion"),
    # Liquidity sweep / bearish breakdown
    ("liquidity_sweep", "liquidity_sweep"),
    ("breakdown", "liquidity_sweep"),
    # Opening range
    ("opening_range", "opening_range"),
    ("orb_", "opening_range"),
    # Momentum continuation — generic breakout / gap-and-go
    ("gap_fill", "momentum_continuation"),
    ("gap_and_go", "momentum_continuation"),
    ("gap_continuation", "momentum_continuation"),
    ("ema_stack", "momentum_continuation"),
    ("ema_cross", "momentum_continuation"),
    ("breakout", "momentum_continuation"),
    # Reversal as a last-resort match (divergence+reversal already caught above)
    ("reversal", "mean_reversion"),
]


def _classify_by_name(name: str | None, description: str | None) -> str | None:
    """Return the family for a pattern by keyword match against name +
    description. Returns None when no rule fires."""
    blob = ((name or "") + " " + (description or "")).lower()
    if not blob.strip():
        return None
    for kw, fam in _NAME_RULES:
        if kw in blob:
            return fam
    return None


def _resolve_via_parent_chain(
    db: Session, pattern_id: int, max_depth: int = 10
) -> str | None:
    """Walk the parent chain looking for a non-NULL hypothesis_family."""
    seen: set[int] = set()
    cur = pattern_id
    depth = 0
    while cur is not None and depth < max_depth:
        if cur in seen:
            return None  # cycle guard (shouldn't happen)
        seen.add(cur)
        row = db.execute(text(
            "SELECT parent_id, hypothesis_family FROM scan_patterns "
            "WHERE id = :p LIMIT 1"
        ), {"p": cur}).fetchone()
        if row is None:
            return None
        if row[1] is not None and row[1] != "unknown":
            return str(row[1])
        cur = row[0]
        depth += 1
    return None


def backfill_pattern_families(
    db: Session, *, dry_run: bool = True
) -> dict[str, Any]:
    """Find NULL/unknown hypothesis_family rows and assign families.

    Returns counts dict + per-pattern proposals (only included when
    dry_run is True; persisted assignments aren't echoed back to keep
    the response compact).
    """
    rows = db.execute(text(
        "SELECT id, name, description, parent_id, lifecycle_stage "
        "FROM scan_patterns "
        "WHERE hypothesis_family IS NULL OR hypothesis_family = 'unknown' "
        "ORDER BY id"
    )).fetchall()

    proposals: list[dict[str, Any]] = []
    by_family: dict[str, int] = {}
    by_method: dict[str, int] = {}
    unresolved: list[int] = []

    for r in rows:
        pid, name, desc, parent_id, stage = r[0], r[1], r[2], r[3], r[4]
        family: str | None = None
        method: str | None = None

        # Pass 1: inherit from parent chain when there is a parent
        if parent_id is not None:
            family = _resolve_via_parent_chain(db, parent_id)
            if family is not None:
                method = "parent_chain"

        # Pass 2: classify by name keywords
        if family is None:
            family = _classify_by_name(name, desc)
            if family is not None:
                method = "name_keyword"

        if family is None:
            unresolved.append(int(pid))
            continue

        proposals.append({
            "id": int(pid),
            "name": name,
            "lifecycle": stage,
            "family": family,
            "method": method,
        })
        by_family[family] = by_family.get(family, 0) + 1
        by_method[method or "unknown"] = by_method.get(method or "unknown", 0) + 1

    summary: dict[str, Any] = {
        "total_null_or_unknown": len(rows),
        "resolved": len(proposals),
        "unresolved": len(unresolved),
        "by_family": by_family,
        "by_method": by_method,
        "dry_run": dry_run,
    }

    if dry_run:
        # Echo proposals so the operator can review before writing.
        summary["proposals"] = proposals
        summary["unresolved_ids"] = unresolved[:20]
        return summary

    # Apply assignments.
    applied = 0
    for p in proposals:
        try:
            db.execute(text(
                "UPDATE scan_patterns SET hypothesis_family = :f "
                "WHERE id = :i AND "
                "      (hypothesis_family IS NULL OR hypothesis_family = 'unknown')"
            ), {"f": p["family"], "i": p["id"]})
            applied += 1
        except Exception as e:
            logger.warning(
                "[family_backfill] update id=%s failed: %s", p["id"], e
            )
    db.commit()
    summary["applied"] = applied
    logger.info("[family_backfill] applied %d family assignments", applied)
    return summary
