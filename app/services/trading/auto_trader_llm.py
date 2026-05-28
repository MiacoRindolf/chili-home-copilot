"""LLM revalidation gate for AutoTrader v1 — strict JSON, fail-closed."""
from __future__ import annotations

import json
import logging
import hashlib
import math
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ...models.trading import BreakoutAlert
from ..llm_caller import call_llm
from .auto_trader_rules import alert_confidence_from_score, projected_profit_pct

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "auto_trader_revalidation.txt"
_PROMPT_VERSION = "autotrader-revalidation-v1"
_REVALIDATION_CACHE_MAX = 512
_REVALIDATION_CACHE_TTL = 300
_revalidation_cache_lock = threading.Lock()
_revalidation_cache: "OrderedDict[str, tuple[float, bool, dict[str, Any]]]" = OrderedDict()
_revalidation_cache_stats = {"hits": 0, "misses": 0, "evictions": 0}


def _hash_material(value: Any) -> str:
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        blob = str(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _price_step(reference_price: float | None) -> float:
    try:
        p = abs(float(reference_price or 0.0))
    except Exception:
        p = 0.0
    if p <= 0:
        return 0.001
    magnitude = 10 ** math.floor(math.log10(max(p, 1e-6)))
    return max(magnitude * 0.0025, 1e-6)


def _bucket_price(value: float | None, reference_price: float | None) -> float | None:
    if value is None:
        return None
    try:
        step = _price_step(reference_price)
        return round(float(value) / step) * step
    except Exception:
        return None


def _cache_key(
    alert: BreakoutAlert,
    *,
    current_price: float,
    ohlcv_summary: str | None,
    pattern_name: str | None,
) -> str:
    reference = float(current_price or alert.entry_price or 0.0)
    scorecard = (alert.indicator_snapshot or {}).get("imminent_scorecard")
    material = {
        "prompt_version": _PROMPT_VERSION,
        "alert_id": getattr(alert, "id", None),
        "ticker": (alert.ticker or "").upper(),
        "pattern_name": pattern_name or "",
        "entry": _bucket_price(alert.entry_price, reference),
        "stop": _bucket_price(alert.stop_loss, reference),
        "target": _bucket_price(alert.target_price, reference),
        "current_price": _bucket_price(current_price, reference),
        "score_at_alert": round(float(alert.score_at_alert or 0.0), 3),
        "scorecard_hash": _hash_material(scorecard),
        "ohlcv_hash": _hash_material(ohlcv_summary or ""),
    }
    return _hash_material(material)


def _cache_get(key: str) -> tuple[bool, dict[str, Any]] | None:
    now = time.monotonic()
    with _revalidation_cache_lock:
        entry = _revalidation_cache.get(key)
        if entry is None:
            _revalidation_cache_stats["misses"] += 1
            return None
        expiry, viable, snapshot = entry
        if expiry < now:
            _revalidation_cache.pop(key, None)
            _revalidation_cache_stats["misses"] += 1
            _revalidation_cache_stats["evictions"] += 1
            return None
        _revalidation_cache.move_to_end(key)
        _revalidation_cache_stats["hits"] += 1
        snap = dict(snapshot)
        snap["llm_revalidation_cache_hit"] = True
        return viable, snap


def _cache_put(key: str, viable: bool, snapshot: dict[str, Any]) -> None:
    if not snapshot:
        return
    expiry = time.monotonic() + _REVALIDATION_CACHE_TTL
    with _revalidation_cache_lock:
        _revalidation_cache[key] = (expiry, bool(viable), dict(snapshot))
        _revalidation_cache.move_to_end(key)
        while len(_revalidation_cache) > _REVALIDATION_CACHE_MAX:
            _revalidation_cache.popitem(last=False)
            _revalidation_cache_stats["evictions"] += 1


def get_revalidation_cache_stats() -> dict[str, Any]:
    with _revalidation_cache_lock:
        hits = _revalidation_cache_stats["hits"]
        misses = _revalidation_cache_stats["misses"]
        evictions = _revalidation_cache_stats["evictions"]
        size = len(_revalidation_cache)
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "evictions": evictions,
        "size": size,
        "hit_rate": round((hits / total) if total else 0.0, 4),
        "ttl_seconds": _REVALIDATION_CACHE_TTL,
        "max_entries": _REVALIDATION_CACHE_MAX,
    }


def reset_revalidation_cache() -> None:
    with _revalidation_cache_lock:
        _revalidation_cache.clear()
        _revalidation_cache_stats["hits"] = 0
        _revalidation_cache_stats["misses"] = 0
        _revalidation_cache_stats["evictions"] = 0


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _strip_code_fence(s: str) -> str:
    cleaned = (s or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


def parse_revalidation_response(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    cleaned = _strip_code_fence(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("[autotrader_llm] JSON parse failed len=%d", len(cleaned))
        return None
    if not isinstance(obj, dict):
        return None
    if "viable" not in obj:
        return None
    return obj


def run_revalidation_llm(
    alert: BreakoutAlert,
    *,
    current_price: float,
    ohlcv_summary: str | None = None,
    pattern_name: str | None = None,
    trace_id: str = "autotrader-revalidation",
) -> tuple[bool, dict[str, Any]]:
    """Return (viable, snapshot) where snapshot includes raw keys or error."""
    system = _load_system_prompt()
    ppp = projected_profit_pct(alert.entry_price, alert.target_price)
    user_payload = {
        "ticker": alert.ticker,
        "pattern_name": pattern_name or "",
        "entry_price": alert.entry_price,
        "stop_loss": alert.stop_loss,
        "take_profit": alert.target_price,
        "current_price": current_price,
        "projected_profit_pct": ppp,
        "confidence_from_score": alert_confidence_from_score(alert),
        "scorecard": (alert.indicator_snapshot or {}).get("imminent_scorecard"),
        "ohlcv_summary": ohlcv_summary or "",
    }
    cache_key = _cache_key(
        alert,
        current_price=current_price,
        ohlcv_summary=ohlcv_summary,
        pattern_name=pattern_name,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    messages = [{"role": "user", "content": json.dumps(user_payload, default=str)}]
    result = call_llm(
        messages,
        max_tokens=256,
        trace_id=trace_id,
        cacheable=False,
        system_prompt=system,
        purpose="autotrader_revalidation",
        return_meta=True,
    )
    if isinstance(result, dict):
        raw = result.get("reply", "")
        gateway_log_id = result.get("gateway_log_id")
    else:
        raw = result
        gateway_log_id = None
    raw = raw if isinstance(raw, str) else str(raw or "")
    if not raw.strip():
        snap: dict[str, Any] = {"error": "llm_unavailable", "raw_preview": ""}
        if gateway_log_id is not None:
            snap["gateway_log_id"] = gateway_log_id
        return False, snap
    parsed = parse_revalidation_response(raw)
    if parsed is None:
        snap = {"error": "parse_failed", "raw_preview": (raw or "")[:500]}
        if gateway_log_id is not None:
            snap["gateway_log_id"] = gateway_log_id
        _cache_put(cache_key, False, snap)
        return False, snap

    viable = bool(parsed.get("viable"))
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(parsed.get("reason", ""))[:500]
    snap = {"viable": viable, "confidence": conf, "reason": reason, "raw": parsed}
    if gateway_log_id is not None:
        snap["gateway_log_id"] = gateway_log_id
    _cache_put(cache_key, viable, snap)
    return viable, snap
