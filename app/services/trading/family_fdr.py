"""Benjamini-Hochberg family-level FDR control for promotion-gate evaluation.

Phase E of f-evidence-fidelity-architecture (2026-05-14). Companion to
:func:`promotion_gate._count_variants_in_family`. DSR (Bailey & López de
Prado 2014) deflates an individual Sharpe by an expected-maximum under
the null of ``n_hypotheses_tested`` independent trials; that is the right
correction for the *single* candidate being evaluated, but it does not
control the false-discovery rate when many siblings of the same
hypothesis family are admitted one at a time.

Harvey-Liu-Zhu (2016) "...and the Cross-Section of Expected Returns"
shows the BH (Benjamini-Hochberg 1995) procedure is the family-wise
control that matches how a research process actually generates trial
candidates: serially, with each new candidate inheriting the
multiple-testing burden of every sibling tested before it.

The math:

    Given a target FDR level ``alpha`` (e.g. ``alpha = 1 - 0.95 = 0.05``)
    and ``m`` siblings already tested in the family, the BH-adjusted
    rejection threshold for the *most significant* (rank 1 of m)
    p-value is ``alpha / m``. We express the threshold on the DSR scale
    (a probability in [0,1] where larger = more significant) by
    inverting: ``adjusted_dsr_threshold = 1 - (alpha / m)``.

    The result is monotone in ``m``: larger family → stricter threshold.
    With ``m = 1`` the function is a no-op (returns the naive
    threshold). The drought operator was concerned about cannot worsen
    asymptotically — see the brief's "drought-floor" argument.

Flag-gated under ``settings.chili_family_fdr_enabled`` (default False).
Shadow-log writes to ``pattern_family_trial_log`` happen regardless of
the flag so operators can observe legacy/adjusted divergence for 7 days
before flipping.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ...config import settings

logger = logging.getLogger(__name__)


def family_fdr_enabled() -> bool:
    """Cheap predicate. Default False — BH adjustment is a no-op at merge."""
    return bool(getattr(settings, "chili_family_fdr_enabled", False))


def bh_adjusted_dsr_threshold(naive_threshold: float, m: int) -> float:
    """Return the Benjamini-Hochberg adjusted DSR threshold for ``m`` siblings.

    The DSR is a [0, 1]-valued probability; we treat ``alpha = 1 -
    naive_threshold`` as the family-wise tail mass we are willing to
    allocate (e.g. ``0.05`` for a naive ``0.95``). BH's most-stringent
    rank-1 threshold is ``alpha / m`` on the p-value scale, hence
    ``1 - alpha / m`` on the DSR scale.

    Edge cases:
      * ``m <= 1`` → returns ``naive_threshold`` unchanged (no family =
        no adjustment).
      * naive in [0, 1] (clamped) — out-of-range inputs are clamped so a
        misconfigured caller cannot push the threshold below the legacy
        floor.

    Math is pure (no DB, no I/O) so it is safe to call from anywhere.
    """
    try:
        m_int = int(m)
    except (TypeError, ValueError):
        return float(naive_threshold)
    if m_int <= 1:
        return float(naive_threshold)
    naive = max(0.0, min(1.0, float(naive_threshold)))
    alpha = max(0.0, 1.0 - naive)
    adj_alpha = alpha / float(m_int)
    return max(0.0, min(1.0, 1.0 - adj_alpha))


def family_best_dsr(sess: Any, hypothesis_family: str) -> Optional[float]:
    """Return the max ``deflated_sharpe`` recorded for ``hypothesis_family``.

    Used to populate ``family_best_dsr_at_time`` on the trial-log row so
    the BH math can be replayed against the active roster after the
    fact. Returns ``None`` on empty family / missing column / DB error
    (graceful degradation — never raises).
    """
    if sess is None or not hypothesis_family:
        return None
    try:
        from sqlalchemy import text as _text
        row = sess.execute(
            _text(
                """
                SELECT MAX(deflated_sharpe)
                FROM scan_patterns
                WHERE hypothesis_family = :fam
                  AND deflated_sharpe IS NOT NULL
                """
            ),
            {"fam": str(hypothesis_family)},
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])
    except Exception as exc:
        logger.debug("[family_fdr] family_best_dsr lookup failed: %s", exc)
        return None


def log_family_trial(
    sess: Any,
    *,
    hypothesis_family: Optional[str],
    variant_pattern_id: int,
    variant_dsr: Optional[float],
    variant_pbo: Optional[float],
    variant_promoted: bool,
    family_variants_tested_so_far: int,
) -> None:
    """Append one row to ``pattern_family_trial_log`` (best-effort).

    Always shadow-writes — the BH adjustment is flag-gated but the
    audit log is not, so the 7-day soak window can compare legacy and
    BH-adjusted verdicts even when the flag is False.

    Never raises: writes fail silently on missing table (pre-migration
    state) or session errors. The caller's transaction is preserved.
    """
    if sess is None or variant_pattern_id is None:
        return
    if not hypothesis_family:
        # Trial-log is keyed by family. Patterns with no family info
        # don't contribute to BH bookkeeping — that's the legacy
        # ``n_hypotheses_tested=1`` floor by construction.
        return
    try:
        from sqlalchemy import text as _text
        best = family_best_dsr(sess, str(hypothesis_family))
        sess.execute(
            _text(
                """
                INSERT INTO pattern_family_trial_log (
                    hypothesis_family,
                    variant_pattern_id,
                    variant_dsr,
                    variant_pbo,
                    variant_promoted,
                    family_best_dsr_at_time,
                    family_variants_tested_so_far
                ) VALUES (
                    :fam, :pid, :dsr, :pbo, :promoted,
                    :best, :mcnt
                )
                """
            ),
            {
                "fam": str(hypothesis_family),
                "pid": int(variant_pattern_id),
                "dsr": (float(variant_dsr) if variant_dsr is not None else None),
                "pbo": (float(variant_pbo) if variant_pbo is not None else None),
                "promoted": bool(variant_promoted),
                "best": (float(best) if best is not None else None),
                "mcnt": int(max(1, family_variants_tested_so_far)),
            },
        )
        try:
            sess.commit()
        except Exception:
            try:
                sess.flush()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("[family_fdr] trial-log write failed: %s", exc)
        try:
            sess.rollback()
        except Exception:
            pass


def family_size_for_pattern(sess: Any, scan_pattern_id: int) -> int:
    """Resolve a candidate's family size from its pattern row.

    Mirror of :func:`promotion_gate._count_variants_in_family` for
    callers that only have the pattern id (e.g.
    :func:`cpcv_adaptive_gate.maybe_apply_adaptive_gate`). One indexed
    lookup; falls back to ``1`` on any error. Reading the column does
    not depend on Phase A's ``corrected_*`` migration — ``hypothesis_family``
    is the existing column added by migration 046.
    """
    if sess is None or scan_pattern_id is None:
        return 1
    try:
        from app.models.trading import ScanPattern
    except Exception:
        return 1
    try:
        pat = sess.get(ScanPattern, int(scan_pattern_id))
    except Exception:
        return 1
    if pat is None:
        return 1
    from .promotion_gate import _count_variants_in_family
    return _count_variants_in_family(sess, pat)
