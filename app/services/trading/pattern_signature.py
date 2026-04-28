"""Stable signature hash for ScanPattern rule sets.

Background (2026-04-28): the trading brain has 614 patterns, 81% of which
are variants of a small handful of parents. Many of those variants are
functionally indistinguishable — same indicators with the same ops and
nearly-identical thresholds, just spawned via different paths (mining,
hypothesis confirmation, variant explorer, web research, builtin
seeds). When the system creates "yet another" near-duplicate it dilutes
the evidence each pattern would otherwise accumulate, increases the
backtest queue load, and makes the operator's audit harder.

This module computes a **stable canonical signature** from a pattern's
``rules_json`` so duplicates can be:

* short-circuited at write time (``ensure_mined_scan_pattern`` and
  friends look up by signature first; if found, increment evidence
  count rather than spawning a row);
* identified retroactively via a backfill (migration 195);
* surfaced to the operator via a "duplicates" report.

Canonicalization rules — keep these stable; the column index uses these
hashes::

  1. parse ``rules_json`` to ``{"conditions": [...]}``; if absent, hash
     the empty marker ``EMPTY_SIGNATURE``.
  2. for each condition dict:
       * keep only the fields {indicator, op, value} (drop comments,
         metadata, source, etc.).
       * canonicalize ``op`` (e.g. ``=`` -> ``==``, strip whitespace).
       * canonicalize ``value`` — bool literals to ``true/false``, ints
         left as ints, floats rounded to 4 decimals (``0.5`` and
         ``0.5000`` collapse), strings lowercased.
       * if ``op`` is one of the comparators ``< <= > >=`` and the
         value is numeric, also round to 4 decimals.
  3. sort the conditions list by ``(indicator, op, value_as_str)``.
  4. join into a stable string and SHA1 hash.

SHA1 is fine here — collision risk for ~1k patterns is negligible and
we're not using it for security. 40-hex-char output fits a fixed-width
column nicely.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

EMPTY_SIGNATURE = "EMPTY"
SIGNATURE_LENGTH = 40  # SHA1 hex digest


def _canonicalize_value(v: Any) -> str:
    """Stable string form of a condition's value."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(int(v))
    if isinstance(v, float):
        # Round to 4 decimals so 0.5 and 0.5000 collapse, but keep finer
        # thresholds distinguishable (e.g. 1.5 vs 1.55 vs 1.50001).
        return f"{round(float(v), 4)}"
    if isinstance(v, (list, tuple)):
        # Range-style values like [40, 65]: order-preserving canonical form.
        return "[" + ",".join(_canonicalize_value(x) for x in v) + "]"
    return str(v).strip().lower()


_CANONICAL_OPS = {
    "=": "==",
    "==": "==",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "!=": "!=",
    "<>": "!=",
    "in": "in",
    "not in": "not in",
    "between": "between",
}


def _canonicalize_condition(cond: dict[str, Any]) -> tuple[str, str, str]:
    indicator = str(cond.get("indicator") or "").strip().lower()
    op_raw = str(cond.get("op") or "").strip().lower()
    op = _CANONICAL_OPS.get(op_raw, op_raw)
    value = _canonicalize_value(cond.get("value"))
    return (indicator, op, value)


def compute_signature(conditions: Iterable[dict[str, Any]] | None) -> str:
    """Return a 40-hex-char stable signature for a list of conditions.

    ``conditions = None`` or empty -> :data:`EMPTY_SIGNATURE`.
    """
    if not conditions:
        return EMPTY_SIGNATURE
    canon = [_canonicalize_condition(c) for c in conditions if isinstance(c, dict)]
    canon = [c for c in canon if c[0]]  # drop conditions with no indicator
    if not canon:
        return EMPTY_SIGNATURE
    canon.sort()
    flat = "|".join(f"{i}:{o}:{v}" for (i, o, v) in canon)
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()


def signature_for_rules_json(rules_json: str | dict | None) -> str:
    """Convenience: parse rules_json and compute the signature."""
    if rules_json is None:
        return EMPTY_SIGNATURE
    obj: Any
    if isinstance(rules_json, dict):
        obj = rules_json
    else:
        try:
            obj = json.loads(rules_json)
        except Exception:
            logger.debug("[pattern_signature] couldn't parse rules_json: %r", str(rules_json)[:80])
            return EMPTY_SIGNATURE
    if not isinstance(obj, dict):
        return EMPTY_SIGNATURE
    return compute_signature(obj.get("conditions") or [])


def signature_for_pattern(pattern: Any) -> str:
    """Compute signature from a ScanPattern ORM instance."""
    return signature_for_rules_json(getattr(pattern, "rules_json", None))


def find_existing_by_signature(sess: Any, signature: str) -> Any:
    """Return the oldest ScanPattern row matching ``signature`` (or None).

    Returning oldest is intentional: if a pattern has been around a while
    and accumulating evidence, that's the canonical row we want to fold
    new duplicates into.
    """
    from sqlalchemy import text
    if not signature or signature == EMPTY_SIGNATURE:
        return None
    row = sess.execute(text(
        "SELECT id FROM scan_patterns WHERE condition_signature = :sig "
        "ORDER BY created_at ASC, id ASC LIMIT 1"
    ), {"sig": signature}).fetchone()
    if row is None:
        return None
    from ...models.trading import ScanPattern
    return sess.query(ScanPattern).get(int(row.id))
