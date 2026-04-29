"""Web Pattern Researcher — autonomous pattern discovery from the internet.

Searches trading education sites, forums, research publications, and blogs
for new breakout/technical-analysis patterns, then uses the LLM to parse
them into ScanPattern DSL rules and backtests them.

Designed to run periodically in the background as part of the learning cycle
or as a standalone scheduler job.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any


def _safe_fraction_from_pct(pct_value: float | None) -> float | None:
    """Convert a percent (0-100) value to fraction (0-1) with NaN/inf guard.

    Source columns ``win_rate`` and ``oos_win_rate`` on ``scan_patterns`` are
    fraction-typed and CHECK-constrained to [0, 1] (see migration 193). The
    upstream backtest result dict reports win_rate in percent units (line 522
    ``if wr > 50: wins += 1`` proves it). Without this conversion the patch
    would either violate the CHECK or — for ``oos_win_rate`` which has no
    constraint — silently store percent-as-fraction (the 2026-04-28 audit
    finding). Also guards against NaN: ``round(float('nan'), 2)`` returns
    ``nan`` which would otherwise leak into the column.
    """
    if pct_value is None:
        return None
    try:
        v = float(pct_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(max(0.0, min(1.0, v / 100.0)), 4)


def _safe_pct(pct_value: float | None) -> float | None:
    """Round a percent value (e.g. avg_return_pct) with NaN/inf guard."""
    if pct_value is None:
        return None
    try:
        v = float(pct_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, 2)

import requests
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import ScanPattern
from ..llm_caller import call_llm
from .pattern_engine import create_pattern, list_patterns

# ──────────────────────────────────────────────────────────────────────────
# P1.3 production wiring — walk-forward aggregation constants
# ──────────────────────────────────────────────────────────────────────────
# Minimum number of tickers that must return ``ok=True`` from
# ``run_walk_forward`` before we're willing to call a pattern-level verdict.
# Below this floor we return ``None`` (pending) so the gate tri-state picks
# ``pending_walk_forward`` + ``allow_active=True`` rather than forcing a
# hard reject on insufficient evidence.
_WALK_FORWARD_MIN_TICKERS = 2

# Simple-majority threshold across successful per-ticker walk-forward runs.
# Intentionally more permissive than ``chili_walk_forward_min_pass_fraction``
# (which is FOLD-level, 0.6 default). Applying 0.6 at both layers would
# produce a joint ~0.36 bar — too harsh for a 5-ticker test panel. Majority
# (≥0.5) across tickers ∧ 0.6 within-ticker gives a ~0.3 joint pass-rate
# which empirically matches operator expectations for "works across regimes".
_WALK_FORWARD_MIN_TICKER_PASS_FRACTION = 0.5

logger = logging.getLogger(__name__)

# ── Search topics rotated across runs ──────────────────────────────────

_RESEARCH_QUERIES: list[str] = [
    "breakout trading pattern technical analysis 2025 2026",
    "momentum breakout setup stock screening strategy",
    "resistance retest consolidation breakout pattern",
    "RSI EMA stack breakout continuation pattern",
    "volume contraction pattern VCP Minervini setup",
    "Bollinger Band squeeze breakout strategy technical",
    "VWAP reclaim institutional buying pattern",
    "crypto breakout pattern altcoin technical analysis",
    "narrow range NR7 breakout volatility contraction",
    "ADX trend strength breakout confirmation strategy",
    "MACD divergence breakout reversal pattern",
    "relative volume surge breakout signal",
    "flag pennant wedge chart pattern breakout rules",
    "cup and handle breakout technical analysis",
    "Fibonacci retracement breakout entry strategy",
    "supply demand zone breakout institutional pattern",
    "opening range breakout intraday strategy rules",
    "pivot point breakout strategy day trading",
    "supertrend indicator breakout filter",
    "Ichimoku cloud breakout Kumo twist signal",
    "mean reversion versus momentum which works 2025",
    "quantitative trading pattern backtesting results published",
    "best screener filter settings for swing breakouts",
    "EMA ribbon expansion breakout signal setup",
]

_last_query_index: int = 0
_last_research_time: float = 0
_MIN_RESEARCH_INTERVAL_S = 3600  # at most once per hour

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_STRIP_HTML_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s{2,}")


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = _STRIP_HTML_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _fetch_page_text(url: str, timeout: int = 10, max_chars: int = 8000) -> str:
    """Fetch a URL and return stripped plain text content."""
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return ""
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ""
        return _strip_html(resp.text[:max_chars * 3])[:max_chars]
    except Exception:
        return ""


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Core research flow ─────────────────────────────────────────────────

def research_new_patterns(
    db: Session,
    max_searches: int = 3,
    max_pages_per_search: int = 2,
    auto_backtest: bool = True,
) -> dict[str, Any]:
    """Search the web for new trading patterns and create ScanPattern entries.

    Returns a report dict with counts of searches, pages read, patterns found.
    """
    global _last_query_index, _last_research_time

    now = time.time()
    if now - _last_research_time < _MIN_RESEARCH_INTERVAL_S:
        return {"skipped": True, "reason": "cooldown"}
    _last_research_time = now

    from ... import web_search as ws

    existing_patterns = list_patterns(db)
    existing_names = {p["name"].lower() for p in existing_patterns}
    existing_descriptions = {
        _content_hash(p.get("description", ""))
        for p in existing_patterns
        if p.get("description")
    }

    report: dict[str, Any] = {
        "searches": 0,
        "pages_read": 0,
        "patterns_extracted": 0,
        "patterns_created": 0,
        "patterns_skipped_duplicate": 0,
        "backtests_run": 0,
        "sources": [],
    }

    queries_to_run = []
    for _ in range(max_searches):
        q = _RESEARCH_QUERIES[_last_query_index % len(_RESEARCH_QUERIES)]
        _last_query_index += 1
        queries_to_run.append(q)

    all_page_texts: list[dict[str, str]] = []

    for query in queries_to_run:
        try:
            results = ws.search(query, max_results=5, trace_id="pattern_research")
            report["searches"] += 1
        except Exception as e:
            logger.warning("[web_research] Search failed for %r: %s", query, e)
            continue

        if not results:
            continue

        for r in results[:max_pages_per_search]:
            url = r.get("href", "")
            snippet = r.get("body", "")
            title = r.get("title", "")

            page_text = _fetch_page_text(url) if url else ""
            report["pages_read"] += 1

            content = page_text if len(page_text) > 200 else snippet
            if not content or len(content) < 50:
                continue

            all_page_texts.append({
                "title": title,
                "url": url,
                "content": content[:4000],
            })

    if not all_page_texts:
        logger.info("[web_research] No usable content found from %d searches", report["searches"])
        return report

    combined_content = "\n\n---\n\n".join(
        f"SOURCE: {p['title']}\nURL: {p['url']}\n{p['content']}"
        for p in all_page_texts[:6]
    )

    extracted = _extract_patterns_from_content(combined_content, existing_names)
    report["patterns_extracted"] = len(extracted)

    for pat_data in extracted:
        name_lower = pat_data.get("name", "").lower()
        desc_hash = _content_hash(pat_data.get("description", ""))

        if name_lower in existing_names or desc_hash in existing_descriptions:
            report["patterns_skipped_duplicate"] += 1
            continue

        try:
            pattern = create_pattern(db, {
                "name": pat_data["name"],
                "description": pat_data.get("description", ""),
                "rules_json": json.dumps({"conditions": pat_data.get("conditions", [])}),
                "origin": "web_discovered",
                "asset_class": pat_data.get("asset_class", "all"),
                "score_boost": pat_data.get("score_boost", 1.0),
                "min_base_score": pat_data.get("min_base_score", 4.0),
                "confidence": 0.2,
                "active": True,
            })
            existing_names.add(name_lower)
            existing_descriptions.add(desc_hash)
            report["patterns_created"] += 1
            report["sources"].append({
                "pattern": pat_data["name"],
                "urls": [p["url"] for p in all_page_texts[:3]],
            })

            logger.info(
                "[web_research] Created web-discovered pattern: %s (id=%d)",
                pat_data["name"], pattern.id,
            )

            if auto_backtest:
                try:
                    _quick_backtest_pattern(db, pattern)
                    report["backtests_run"] += 1
                except Exception:
                    pass

        except Exception as e:
            logger.warning("[web_research] Failed to create pattern %r: %s", pat_data.get("name"), e)

    logger.info(
        "[web_research] Research complete: %d searches, %d pages, "
        "%d extracted, %d created, %d duplicates skipped",
        report["searches"], report["pages_read"],
        report["patterns_extracted"], report["patterns_created"],
        report["patterns_skipped_duplicate"],
    )
    return report


def _extract_patterns_from_content(
    content: str,
    existing_names: set[str],
) -> list[dict[str, Any]]:
    """Use LLM to extract tradable pattern definitions from web content."""
    existing_list = ", ".join(list(existing_names)[:15]) if existing_names else "(none)"

    prompt = (
        "You are a quantitative trading analyst. Read the following web content about "
        "trading patterns and extract DISTINCT, actionable breakout/technical patterns.\n\n"
        "For each pattern found, define it as a structured JSON rule set that a scanner "
        "can evaluate mechanically.\n\n"
        f"## Web Content:\n{content[:6000]}\n\n"
        f"## Already Known Patterns (DO NOT duplicate these):\n{existing_list}\n\n"
        "## Available Indicators for conditions:\n"
        "rsi_14, ema_20, ema_50, ema_100, price, bb_squeeze, adx, rel_vol, "
        "macd_hist, resistance_retests, dist_to_resistance_pct, narrow_range, "
        "vcp_count, vwap_reclaim\n\n"
        "## Available operators: >, >=, <, <=, ==, between, any_of\n"
        "For price vs indicator comparisons, use 'ref' key pointing to indicator name.\n\n"
        "## Output format — respond ONLY with a JSON array:\n"
        "[\n"
        "  {\n"
        '    "name": "Short unique pattern name",\n'
        '    "description": "1-2 sentence description of when/why this pattern works",\n'
        '    "asset_class": "all" or "stocks" or "crypto",\n'
        '    "conditions": [\n'
        '      {"indicator": "rsi_14", "op": ">", "value": 50},\n'
        '      {"indicator": "price", "op": ">", "ref": "ema_20"}\n'
        "    ],\n"
        '    "score_boost": 1.5,\n'
        '    "min_base_score": 4.0\n'
        "  }\n"
        "]\n\n"
        "RULES:\n"
        "- Only extract patterns with clear, measurable entry conditions\n"
        "- Skip vague or subjective patterns that can't be coded\n"
        "- Maximum 3 patterns per response\n"
        "- Each pattern must have at least 2 conditions\n"
        "- Return an empty array [] if nothing new/actionable is found\n"
        "- Respond with ONLY the JSON array, no other text"
    )

    try:
        response = call_llm(
            messages=[
                {"role": "system", "content": (
                    "You are a precise technical analyst and pattern extraction engine. "
                    "You convert qualitative trading knowledge into quantitative rule sets."
                )},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1500,
            trace_id="web_pattern_extract",
            cacheable=True,
        )

        if not response:
            return []

        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        patterns = json.loads(text)
        if not isinstance(patterns, list):
            patterns = [patterns]

        valid = []
        for p in patterns:
            name = p.get("name", "").strip()
            conditions = p.get("conditions", [])
            if not name or len(conditions) < 2:
                continue
            if name.lower() in existing_names:
                continue
            all_valid = True
            for c in conditions:
                if not c.get("indicator") or not c.get("op"):
                    all_valid = False
                    break
                if c.get("value") is None and not c.get("ref"):
                    all_valid = False
                    break
            if all_valid:
                valid.append(p)

        return valid[:3]

    except (json.JSONDecodeError, TypeError):
        logger.warning("[web_research] Failed to parse LLM response as JSON")
        return []
    except Exception as e:
        logger.warning("[web_research] Pattern extraction failed: %s", e)
        return []


def _walk_forward_verdict_for_pattern(
    pattern: ScanPattern,
    tickers: list[str],
    *,
    interval: str,
) -> tuple[bool | None, dict[str, Any]]:
    """Aggregate per-ticker walk-forward verdicts into a pattern-level gate input.

    Returns ``(verdict, audit)`` where:

    * ``verdict`` is the tri-state value passed into
      ``brain_apply_oos_promotion_gate(walk_forward_passes_gate=...)``:
          ``True``   — majority of tickers that ran WF passed their gate
          ``False``  — not enough tickers passed (hard reject)
          ``None``   — flag OFF, WF failed to run on enough tickers, or any
                      exception — lets the gate tri-state to
                      ``pending_walk_forward`` / ``allow_active=True`` rather
                      than rejecting on missing evidence
    * ``audit`` is a dict suitable to merge into ``oos_validation_json``
      for operator forensics. Always present, even when the verdict is
      ``None``, so the reason for a pending/skip decision is recoverable.

    Fail-open on every exception: we never let a walk-forward problem
    derail the existing promotion flow. The worst-case for a buggy WF run
    is the pattern is evaluated exactly as it would have been pre-P1.3.

    Short-circuits on ``chili_walk_forward_enabled=False`` BEFORE importing
    anything or fetching OHLCV. This is critical — the flag defaults OFF,
    so every discovered pattern hits this function, and paying a 5-ticker
    network fetch per call for a no-op default would exhaust the Windows
    ephemeral-socket pool under load (same root cause we hit in P1.4's
    first wiring pass; see that section of the plan for the post-mortem).
    """
    if not bool(getattr(settings, "chili_walk_forward_enabled", False)):
        return None, {"enabled": False, "reason": "flag_off"}

    audit: dict[str, Any] = {
        "enabled": True,
        "tickers_requested": list(tickers),
        "tickers_ran": 0,
        "tickers_passed": 0,
        "per_ticker": [],
    }
    try:
        # Local import — keep the module import-time surface clean even
        # when walk-forward is shipped-but-idle. The import is trivial but
        # we don't want to re-run it per discovered pattern.
        from ..backtest_service import run_walk_forward

        conditions: list[dict[str, Any]] = []
        try:
            rules = json.loads(pattern.rules_json) if pattern.rules_json else {}
            conditions = rules.get("conditions", []) if isinstance(rules, dict) else []
        except (json.JSONDecodeError, TypeError):
            conditions = []
        if not conditions:
            audit["reason"] = "no_conditions"
            return None, audit

        for ticker in tickers:
            per_ticker: dict[str, Any] = {"ticker": ticker, "ok": False, "passes_gate": None}
            try:
                wf = run_walk_forward(
                    ticker=ticker,
                    conditions=conditions,
                    pattern_name=pattern.name,
                    interval=interval,
                )
                per_ticker["ok"] = bool(wf.get("ok"))
                per_ticker["passes_gate"] = wf.get("passes_gate")
                per_ticker["gate_reason"] = wf.get("gate_reason")
                agg = wf.get("aggregate") or {}
                per_ticker["n_folds"] = agg.get("n_folds")
                per_ticker["n_passes"] = agg.get("n_passes")
                per_ticker["pass_fraction"] = agg.get("pass_fraction")
                per_ticker["mean_test_win_rate"] = agg.get("mean_test_win_rate")
                if per_ticker["ok"]:
                    audit["tickers_ran"] += 1
                    if per_ticker["passes_gate"] is True:
                        audit["tickers_passed"] += 1
            except Exception as e:
                per_ticker["error"] = f"{type(e).__name__}: {e}"
            audit["per_ticker"].append(per_ticker)

        n_ran = int(audit["tickers_ran"])
        n_passed = int(audit["tickers_passed"])
        audit["n_ran"] = n_ran
        audit["n_passed"] = n_passed

        if n_ran < _WALK_FORWARD_MIN_TICKERS:
            audit["reason"] = "insufficient_ticker_coverage"
            audit["verdict"] = None
            return None, audit

        pass_fraction = n_passed / n_ran if n_ran > 0 else 0.0
        audit["ticker_pass_fraction"] = round(pass_fraction, 3)
        audit["ticker_pass_threshold"] = _WALK_FORWARD_MIN_TICKER_PASS_FRACTION

        if pass_fraction >= _WALK_FORWARD_MIN_TICKER_PASS_FRACTION:
            audit["reason"] = "majority_passed"
            audit["verdict"] = True
            return True, audit
        else:
            audit["reason"] = "majority_failed"
            audit["verdict"] = False
            return False, audit
    except Exception as e:
        logger.warning("[web_research] walk-forward aggregation failed: %s", e)
        audit["reason"] = f"exception:{type(e).__name__}"
        audit["verdict"] = None
        return None, audit


def _quick_backtest_pattern(db: Session, pattern: ScanPattern) -> None:
    """Run a quick backtest on the newly discovered pattern and update confidence."""
    from ..backtest_service import (
        backtest_metrics_for_promotion_gate,
        backtest_pattern,
        get_backtest_params,
        save_backtest,
    )
    from .pattern_engine import update_pattern
    from .learning import (
        _find_insight_for_pattern,
        brain_apply_oos_promotion_gate,
        brain_oos_gate_kwargs_for_pattern,
        brain_pattern_backtest_friction_kwargs,
    )

    linked_insight = _find_insight_for_pattern(db, pattern)

    tf = getattr(pattern, "timeframe", "1d") or "1d"
    bt_params = get_backtest_params(tf)
    bt_kw = brain_pattern_backtest_friction_kwargs(db)

    test_tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "BTC-USD"]
    wins = 0
    total = 0
    returns: list[float] = []
    is_wrs: list[float] = []
    oos_wrs: list[float] = []
    oos_rets: list[float] = []
    oos_ticker_hits = 0
    oos_trade_sum = 0
    integrity_rows: list[dict[str, Any]] = []

    for ticker in test_tickers:
        try:
            result = backtest_pattern(
                ticker=ticker,
                pattern_name=pattern.name,
                rules_json=pattern.rules_json,
                interval=bt_params["interval"],
                period=bt_params["period"],
                exit_config=getattr(pattern, "exit_config", None),
                scan_pattern_id=pattern.id,
                **bt_kw,
            )
            if not result.get("ok"):
                continue
            total += 1
            wr, ret_pct = backtest_metrics_for_promotion_gate(result)
            is_wrs.append(wr)
            if wr > 50:
                wins += 1
            returns.append(ret_pct)
            if result.get("oos_ok") and result.get("oos_win_rate") is not None:
                oos_ticker_hits += 1
                oos_wrs.append(float(result["oos_win_rate"]))
                oos_trade_sum += int(result.get("oos_trade_count") or 0)
                if result.get("oos_return_pct") is not None:
                    oos_rets.append(float(result["oos_return_pct"]))
            if linked_insight:
                try:
                    save_backtest(
                        db,
                        linked_insight.user_id,
                        result,
                        insight_id=linked_insight.id,
                        scan_pattern_id=pattern.id,
                    )
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
            ri = result.get("research_integrity")
            if isinstance(ri, dict):
                integrity_rows.append({"ticker": ticker, **ri})
        except Exception:
            continue

    if total > 0:
        ticker_vote_wr = (wins / total) * 100
        mean_is_wr = sum(is_wrs) / len(is_wrs) if is_wrs else 0.0
        mean_oos_wr = sum(oos_wrs) / len(oos_wrs) if oos_wrs else None
        mean_oos_ret = sum(oos_rets) / len(oos_rets) if oos_rets else None
        avg_return = sum(returns) / len(returns) if returns else 0
        confidence = max(0.1, min(0.8, ticker_vote_wr / 100))

        # P1.3 production wiring — run date-based walk-forward on each
        # ticker and fold the aggregate verdict into the gate. Flag-off
        # (``chili_walk_forward_enabled=False``, the default) short-circuits
        # to ``None`` and the gate treats it as a pass-through, preserving
        # legacy behavior for unwired environments.
        wf_verdict, wf_audit = _walk_forward_verdict_for_pattern(
            pattern,
            test_tickers,
            interval=bt_params["interval"],
        )

        _oos_kw = brain_oos_gate_kwargs_for_pattern(pattern, oos_trade_sum)
        prom_stat, allow_active = brain_apply_oos_promotion_gate(
            origin=getattr(pattern, "origin", "") or "",
            mean_is_win_rate=mean_is_wr,
            mean_oos_win_rate=mean_oos_wr,
            oos_tickers_with_result=oos_ticker_hits,
            walk_forward_passes_gate=wf_verdict,
            **_oos_kw,
        )

        from .research_integrity import (
            aggregate_promotion_integrity,
            promotion_blocked_by_integrity,
        )

        prev_ov = getattr(pattern, "oos_validation_json", None) or {}
        if not isinstance(prev_ov, dict):
            prev_ov = {}
        ri_agg = aggregate_promotion_integrity(integrity_rows)
        oos_merged = {
            **prev_ov,
            "evaluated_at": datetime.utcnow().isoformat() + "Z",
            "research_integrity": ri_agg,
            "walk_forward": wf_audit,
        }

        # 2026-04-28 audit fix: win_rate / oos_win_rate columns are
        # fraction-typed [0, 1] (CHECK constraint on win_rate from mig 193).
        # Upstream `mean_is_wr` / `mean_oos_wr` are in PERCENT units (line 522
        # `if wr > 50` proves it). Without _safe_fraction_from_pct this either
        # violated the CHECK or — for oos_win_rate which has no constraint
        # yet — silently stored ~65.3 where readers expected ~0.65 (the
        # 30-pattern corruption noted in the 2026-04-28 audit).
        # Also guards against NaN bleeding through (mig 193's bug type).
        patch: dict[str, Any] = {
            "confidence": round(confidence, 3),
            "win_rate": _safe_fraction_from_pct(mean_is_wr),
            "avg_return_pct": _safe_pct(avg_return),
            "backtest_count": total,
            "evidence_count": total,
            "promotion_status": prom_stat,
            "oos_win_rate": _safe_fraction_from_pct(mean_oos_wr),
            "oos_avg_return_pct": _safe_pct(mean_oos_ret),
            "oos_trade_count": oos_trade_sum if oos_trade_sum else None,
            "backtest_spread_used": bt_kw.get("spread"),
            "backtest_commission_used": bt_kw.get("commission"),
            "oos_evaluated_at": datetime.utcnow(),
            "oos_validation_json": oos_merged,
        }
        if promotion_blocked_by_integrity(ri_agg, target_status=str(prom_stat)):
            patch["promotion_status"] = "rejected_research_integrity"
            patch["active"] = False
            oos_merged = dict(oos_merged)
            oos_merged["research_integrity_blocked_promotion"] = True
            patch["oos_validation_json"] = oos_merged
        if confidence < 0.25 and total >= 3:
            patch["active"] = False
            logger.info(
                "[web_research] Deactivated low-confidence web pattern: %s (conf=%.2f)",
                pattern.name, confidence,
            )
        if not allow_active:
            patch["active"] = False
            logger.info(
                "[web_research] OOS gate rejected web pattern: %s (status=%s)",
                pattern.name, prom_stat,
            )
        from .lifecycle import lifecycle_stage_from_promotion_status

        patch["lifecycle_stage"] = lifecycle_stage_from_promotion_status(
            str(patch.get("promotion_status", ""))
        )
        update_pattern(db, pattern.id, patch)


# ── Scheduler entry point ──────────────────────────────────────────────

def run_web_pattern_research(db: Session | None = None) -> dict[str, Any]:
    """Entry point for scheduler / learning cycle integration.

    Creates its own DB session if none provided.
    """
    close_db = False
    if db is None:
        from ...db import SessionLocal
        db = SessionLocal()
        close_db = True

    try:
        report = research_new_patterns(db, max_searches=3, max_pages_per_search=2)
        return report
    except Exception as e:
        logger.error("[web_research] Research cycle failed: %s", e)
        return {"error": str(e)}
    finally:
        if close_db:
            db.close()


def get_research_status() -> dict[str, Any]:
    """Return current research state for the UI."""
    return {
        "last_research": datetime.utcfromtimestamp(_last_research_time).isoformat()
            if _last_research_time > 0 else None,
        "queries_completed": _last_query_index,
        "total_queries": len(_RESEARCH_QUERIES),
        "cooldown_remaining_s": max(0, int(
            _MIN_RESEARCH_INTERVAL_S - (time.time() - _last_research_time)
        )) if _last_research_time > 0 else 0,
    }
