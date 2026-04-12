"""Edge-vs-luck evidence for repeatable-edge ScanPatterns (brain_discovered / web_discovered).

v1 uses weak nulls (explicitly labeled for operators): ticker-exchangeability for IS/OOS
pool stats, and fold-return shuffle when benchmark walk-forward windows exist.

Gating and lifecycle effects are applied in ``learning.test_pattern_hypothesis`` — not in UI.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

CHALLENGE_VERSION = 1

WEAK_NULL_DISCLAIMER = (
    "CHILI v1 weak-null evidence: permutations assume exchangeability of ticker-level "
    "summary stats (IS/OOS) or of walk-forward fold returns — not a full purged trade-sequence null. "
    "Use as a hygiene signal, not publication-grade inference."
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mean(xs: list[float]) -> float:
    if not xs:
        return 0.0
    return float(sum(xs) / len(xs))


def permutation_mean_one_sided_p(
    values: list[float],
    *,
    n_perm: int,
    rng: Any,
) -> tuple[float | None, str | None]:
    """One-sided p-value: how often permuted sample mean >= observed mean (weak exchangeability null)."""
    n = len(values)
    if n < 2:
        return None, "insufficient_units"
    obs = _mean(values)
    if math.isnan(obs):
        return None, "nan_stat"
    count_ge = 0
    work = list(values)
    for _ in range(max(1, int(n_perm))):
        rng.shuffle(work)
        if _mean(work) >= obs - 1e-12:
            count_ge += 1
    p = (1 + count_ge) / (max(1, int(n_perm)) + 1)
    return float(min(1.0, max(0.0, p))), None


def apply_fdr_correction(p_values: list[float | None], q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg FDR control. Returns list of bools (True=significant) aligned with input."""
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    result = [False] * len(p_values)
    if not indexed:
        return result
    indexed.sort(key=lambda x: x[1])
    m = len(indexed)
    # Walk from largest rank down; once one passes, all smaller ranks pass too
    max_rank_passing = -1
    for rank_0, (orig_i, pv) in enumerate(indexed):
        rank = rank_0 + 1  # 1-based
        threshold = q * rank / m
        if pv <= threshold:
            max_rank_passing = rank_0
    # All indices up to max_rank_passing are significant
    for rank_0 in range(max_rank_passing + 1):
        orig_i, _ = indexed[rank_0]
        result[orig_i] = True
    return result


def collect_walk_forward_fold_returns(bench_raw: dict[str, Any] | None, *, embargo_bars: int = 0) -> list[float]:
    """Extract return_pct from benchmark_walk_forward_evaluate raw tickers[*].windows.

    When *embargo_bars* > 0, the first *embargo_bars* OOS windows per ticker are
    skipped to prevent forward-return leakage from the training window boundary.
    """
    if not bench_raw or not isinstance(bench_raw, dict):
        return []
    out: list[float] = []
    tickers = bench_raw.get("tickers") or {}
    if not isinstance(tickers, dict):
        return []
    for _sym, rec in tickers.items():
        if not isinstance(rec, dict):
            continue
        windows = rec.get("windows") or []
        for wi, w in enumerate(windows):
            if not isinstance(w, dict):
                continue
            if not w.get("ok"):
                continue
            if embargo_bars > 0 and wi < embargo_bars:
                continue  # skip embargoed bars at fold boundary
            rp = w.get("return_pct")
            if rp is None:
                continue
            try:
                out.append(float(rp))
            except (TypeError, ValueError):
                continue
    return out


def build_edge_evidence(
    *,
    mean_is_wr_pct: float,
    is_wrs: list[float],
    mean_oos_wr_pct: float | None,
    oos_wrs: list[float],
    oos_ticker_hits: int,
    tickers_tested: int,
    oos_trade_sum: int,
    bench_raw: dict[str, Any] | None,
    n_perm: int,
    seed: int,
    prev_block_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble ``oos_validation_json['edge_evidence']`` document (persisted)."""
    import random

    rng = random.Random(int(seed))

    is_p, is_skip = permutation_mean_one_sided_p(list(is_wrs), n_perm=n_perm, rng=rng)

    oos_p: float | None = None
    oos_skip: str | None = "no_oos_vector"
    if len(oos_wrs) >= 2:
        oos_p, oos_skip = permutation_mean_one_sided_p(list(oos_wrs), n_perm=n_perm, rng=rng)

    embargo_bars = 5
    folds = collect_walk_forward_fold_returns(bench_raw, embargo_bars=embargo_bars)
    wf_score: float | None = None
    wf_p: float | None = None
    wf_skip: str | None = None
    wf_source = "none"
    if len(folds) >= 3:
        wf_score = _mean(folds)
        wf_p, wf_skip = permutation_mean_one_sided_p(folds, n_perm=n_perm, rng=rng)
        wf_source = "bench_walk_forward_folds"
    elif mean_oos_wr_pct is not None and len(oos_wrs) >= 2:
        # Four-layer contract: when benchmark windows are unavailable, WF slot uses OOS ticker pool
        # (same weak null as oos_p — labeled for operators).
        wf_score = float(mean_oos_wr_pct)
        wf_p = oos_p
        wf_skip = oos_skip
        wf_source = "oos_ticker_pool_proxy_v1"
    else:
        wf_skip = "insufficient_folds"

    eff_n = min(
        max(0, int(oos_trade_sum)),
        max(1, int(tickers_tested)),
    )
    cov: float | None = None
    if tickers_tested > 0:
        cov = round(float(oos_ticker_hits) / float(tickers_tested), 4)

    tier = _evidence_tier(
        is_p=is_p,
        oos_p=oos_p,
        wf_p=wf_p,
        oos_n_units=len(oos_wrs),
        fold_n=len(folds),
        wf_source=wf_source,
    )

    block_codes = list(prev_block_codes) if prev_block_codes else []

    # FDR correction across the three tests
    fdr_sig = apply_fdr_correction([is_p, oos_p, wf_p], q=0.10)

    return {
        "challenge_version": CHALLENGE_VERSION,
        "null_model_is_oos": "v1_ticker_wr_exchangeability",
        "null_model_walk_forward": "v1_fold_return_shuffle",
        "weak_null_disclaimer": WEAK_NULL_DISCLAIMER,
        "in_sample_score": round(float(mean_is_wr_pct), 4),
        "in_sample_perm_p": round(is_p, 6) if is_p is not None else None,
        "in_sample_perm_skip": is_skip,
        "oos_mean_wr_pct": round(float(mean_oos_wr_pct), 4) if mean_oos_wr_pct is not None else None,
        "oos_perm_p": round(oos_p, 6) if oos_p is not None else None,
        "oos_perm_skip": oos_skip,
        "walk_forward_score": round(wf_score, 6) if wf_score is not None else None,
        "walk_forward_perm_p": round(wf_p, 6) if wf_p is not None else None,
        "walk_forward_perm_skip": wf_skip,
        "walk_forward_evidence_source": wf_source,
        "embargo_bars": embargo_bars,
        "fdr_significant_is": fdr_sig[0],
        "fdr_significant_oos": fdr_sig[1],
        "fdr_significant_wf": fdr_sig[2],
        "effective_n": int(eff_n),
        "oos_coverage": cov,
        "evidence_fresh_at": _utc_now_iso(),
        "evidence_tier": tier,
        "promotion_block_codes": block_codes,
    }


def _evidence_tier(
    *,
    is_p: float | None,
    oos_p: float | None,
    wf_p: float | None,
    oos_n_units: int,
    fold_n: int,
    wf_source: str,
) -> str:
    """Coarse tier for desk sorting (deterministic, not a trading signal)."""
    if oos_n_units < 2:
        return "none"
    ok_oos = oos_p is not None and oos_p <= 0.15
    ok_is = is_p is not None and is_p <= 0.15
    wf_ok_n = fold_n >= 3 or wf_source == "oos_ticker_pool_proxy_v1"
    ok_wf = wf_p is None or (wf_ok_n and wf_p <= 0.20)
    if ok_is and ok_oos and ok_wf and oos_n_units >= 3:
        return "A"
    if ok_oos and oos_n_units >= 2:
        return "B"
    if oos_p is not None:
        return "C"
    return "none"


def apply_edge_evidence_veto(
    evidence: dict[str, Any],
    *,
    max_is_perm_p: float | None,
    max_oos_perm_p: float,
    max_wf_perm_p: float,
    require_wf_when_available: bool,
    fdr_enabled: bool = True,
) -> tuple[bool, list[str]]:
    """Return (should_veto, extra_block_codes). Mutates evidence['promotion_block_codes']."""
    codes: list[str] = []
    blocks = list(evidence.get("promotion_block_codes") or [])

    is_p = evidence.get("in_sample_perm_p")
    if (
        max_is_perm_p is not None
        and is_p is not None
        and float(is_p) > float(max_is_perm_p)
    ):
        codes.append("weak_null_in_sample_perm_p")

    oos_p = evidence.get("oos_perm_p")
    if oos_p is not None and float(oos_p) > float(max_oos_perm_p):
        codes.append("weak_null_oos_perm_p")

    wf_p = evidence.get("walk_forward_perm_p")
    wf_skip = evidence.get("walk_forward_perm_skip")
    if wf_p is not None:
        if float(wf_p) > float(max_wf_perm_p):
            codes.append("weak_null_wf_perm_p")
    elif require_wf_when_available and wf_skip not in (
        "insufficient_folds",
        "insufficient_units",
        "no_oos_vector",
        None,
    ):
        # Bench did not yield fold stats — optional strict mode
        codes.append("weak_null_wf_unavailable")

    # FDR gate: if any test was individually significant but fails FDR, flag it
    if fdr_enabled:
        if evidence.get("fdr_significant_oos") is False and oos_p is not None:
            if "weak_null_fdr_oos" not in codes:
                codes.append("weak_null_fdr_oos")
        if evidence.get("fdr_significant_wf") is False and wf_p is not None:
            if "weak_null_fdr_wf" not in codes:
                codes.append("weak_null_fdr_wf")

    if codes:
        for c in codes:
            if c not in blocks:
                blocks.append(c)
        evidence["promotion_block_codes"] = blocks
        return True, codes
    return False, []


def apply_phase2_hygiene_nudges(
    edge_evidence: dict[str, Any],
    *,
    parameter_stability: dict[str, Any] | None,
    selection_bias: dict[str, Any] | None,
    oos_validation: dict[str, Any],
) -> None:
    """Soft tier / flag nudges from Phase 2 (no promotion hard gate). Mutates dicts in place."""
    down_map = {"A": "B", "B": "C", "C": "none", "none": "none"}
    flags = list(oos_validation.get("research_hygiene_flags") or [])
    steps = 0
    ps = parameter_stability if isinstance(parameter_stability, dict) else None
    sb = selection_bias if isinstance(selection_bias, dict) else None
    if ps and ps.get("stability_tier") == "fragile":
        if "phase2_fragile_parameter_neighborhood" not in flags:
            flags.append("phase2_fragile_parameter_neighborhood")
        steps += 1
    if sb and sb.get("burn_tier") == "high" and not sb.get("skip_reason"):
        if "phase2_high_validation_slice_burn" not in flags:
            flags.append("phase2_high_validation_slice_burn")
        steps += 1
    oos_validation["research_hygiene_flags"] = flags
    tier = str(edge_evidence.get("evidence_tier") or "none")
    for _ in range(min(steps, 2)):
        tier = down_map.get(tier, "none")
    edge_evidence["evidence_tier"] = tier


def resolve_gated_lifecycle_stage(
    *,
    promotion_status: str,
    edge_gate_ran: bool,
    edge_veto: bool,
) -> str | None:
    """Return lifecycle_stage override for gated patterns when edge layer ran; else None."""
    if not edge_gate_ran:
        return None
    ps = (promotion_status or "").strip().lower()
    if edge_veto:
        return "challenged"
    if ps == "promoted":
        return "promoted"
    if ps.startswith("rejected"):
        return None
    if ps in ("pending_oos", "backtested", "pending_bench", "legacy"):
        return "validated"
    if ps == "validated":
        return "validated"
    return "candidate"
