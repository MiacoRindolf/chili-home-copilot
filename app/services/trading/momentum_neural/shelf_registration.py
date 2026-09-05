"""Shelf-registration awareness (2026-08-18 Ross recaps).

Bago pumasok si Ross sa isang runner, tinitingnan niya ang REGISTERED-FOR-SALE
shares laban sa displayed float — ang PFSA noong 2026-08-18 ay may 179M
registered sa likod ng 605K displayed float, at ang muted na HOD-break squeeze
ng kanyang big winner ay sinisi niya sa posibleng shelf tapping ("a little
heavier than I thought"). Ang aktibong shelf ay EXPECTATION DAMPER (size-down,
mas mababang extension), HINDI veto — kinatrade pa rin niya pareho.

Disenyo:
- SEC EDGAR, walang key: ticker→CIK mula company_tickers.json (cached ~24h),
  filings mula data.sec.gov/submissions/CIK##########.json (cached bawat
  simbolo ~24h, kasama ang negative cache). Kailangan ng deklaradong
  User-Agent ayon sa EDGAR fair-access policy.
- ANG SIZING AY HINDI KAILANMAN NAG-NENETWORK: ang `prime_shelf_cache(symbol)`
  ay tinatawag sa watch-start (kung saan hindi masakit ang ilang segundong
  latency, minsanan bawat session); ang `cached_shelf_state(symbol)` ang
  binabasa ng sizing at CACHE LANG ang tinitingnan.
- FAIL-OPEN: walang data / network error / hindi kilalang ticker ⇒ None ⇒
  walang damper (ang damper ay nangangailangan ng POSITIBONG ebidensya ng
  aktibong registration).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# EDGAR fair-access: kailangang tunay na makapagpakilala ang User-Agent.
_USER_AGENT = "CHILI-research/1.0 (rindolf.miaco@gmail.com)"
_HTTP_TIMEOUT_S = 4.0

# Ang mga form na bumubuo ng "aktibong shelf / kamakailang registration" na
# maaaring tapyasin ng issuer sa isang squeeze (prefix match).
_SHELF_FORM_PREFIXES = ("S-1", "S-3", "F-1", "F-3", "424B")

_CACHE_TTL_S = 24 * 3600.0

_lock = threading.Lock()
_ticker_map: dict[str, int] | None = None
_ticker_map_at: float = 0.0
# symbol -> (fetched_at_monotonic, state | None)
_state_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _http_get_json(url: str) -> Any:
    import requests

    resp = requests.get(
        url, headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT_S
    )
    resp.raise_for_status()
    return resp.json()


def _load_ticker_map() -> dict[str, int] | None:
    global _ticker_map, _ticker_map_at
    with _lock:
        if _ticker_map is not None and (time.monotonic() - _ticker_map_at) < _CACHE_TTL_S:
            return _ticker_map
    try:
        raw = _http_get_json(_TICKER_MAP_URL)
        mapping: dict[str, int] = {}
        rows = raw.values() if isinstance(raw, dict) else raw
        for row in rows:
            if not isinstance(row, dict):
                continue
            t = str(row.get("ticker") or "").strip().upper()
            cik = row.get("cik_str")
            if t and isinstance(cik, int):
                mapping[t] = cik
        if not mapping:
            return None
        with _lock:
            _ticker_map = mapping
            _ticker_map_at = time.monotonic()
        return mapping
    except Exception as exc:
        logger.debug("[shelf] ticker map fetch failed: %s", exc)
        return None


def _lookback_days() -> float:
    try:
        from ....config import settings

        raw = getattr(settings, "chili_momentum_shelf_lookback_days", 365.0)
        value = 365.0 if raw is None else float(raw)
        return value if value > 0 else 365.0
    except Exception:
        return 365.0


def _fetch_state(symbol: str) -> dict[str, Any] | None:
    """Isang EDGAR round-trip; None sa anumang kabiguan (fail-open)."""
    mapping = _load_ticker_map()
    if not mapping:
        return None
    cik = mapping.get(symbol)
    if cik is None:
        return None
    try:
        subs = _http_get_json(_SUBMISSIONS_URL.format(cik=int(cik)))
        recent = (subs.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=_lookback_days())
        hits: list[dict[str, str]] = []
        for form, date_s in zip(forms, dates):
            form_u = str(form or "").strip().upper()
            if not form_u.startswith(_SHELF_FORM_PREFIXES):
                continue
            try:
                filed = datetime.strptime(str(date_s), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except (TypeError, ValueError):
                continue
            if filed >= cutoff:
                hits.append({"form": form_u, "filed": str(date_s)})
        newest = max((h["filed"] for h in hits), default=None)
        newest_form = next((h["form"] for h in hits if h["filed"] == newest), None)
        return {
            "symbol": symbol,
            "cik": int(cik),
            "shelf_active": bool(hits),
            "shelf_filing_count": len(hits),
            "newest_filing_date": newest,
            # Telemetry (2026-09-02): WHICH form and WHEN this read happened, so a
            # 24 h-old cache entry is auditable against the ledger record.
            "newest_form": newest_form,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "lookback_days": round(_lookback_days(), 1),
        }
    except Exception as exc:
        logger.debug("[shelf] submissions fetch failed for %s: %s", symbol, exc)
        return None


def prime_shelf_cache(symbol: str) -> None:
    """Tawagin sa WATCH-start (latency-tolerant): pinupuno ang cache nang
    minsanan bawat TTL; ang kabiguan ay naka-cache din (negative cache) para
    hindi mag-loop ng network calls."""
    # HERMETIC REPLAY/TEST GUARD (golden-batch triage 2026-08-21): ang EDGAR
    # prime thread na ito ang bumagsak sa replay network fence — bawat window
    # ay namamatay sa "swallowed forbidden network attempt". Sa replay/test,
    # laktawan ang prime: ang sizing ay cache-read lang at fail-open (walang
    # positibong shelf evidence = walang damper) — ang mismong disenyo ng
    # module. Walang pagbabago sa live.
    import os

    if (
        os.environ.get("CHILI_DIAGNOSTIC_REPLAY_ISOLATED") == "true"
        or os.environ.get("CHILI_PYTEST") == "1"
    ):
        return
    sym = str(symbol or "").strip().upper()
    if not sym or sym.endswith("-USD"):
        return
    now = time.monotonic()
    with _lock:
        hit = _state_cache.get(sym)
        if hit is not None and (now - hit[0]) < _CACHE_TTL_S:
            return
    state = _fetch_state(sym)
    with _lock:
        _state_cache[sym] = (time.monotonic(), state)
    if state is not None and state.get("shelf_active"):
        logger.info(
            "[shelf] %s: AKTIBONG registration (%d filings, pinakabago %s) — "
            "expectation damper ang papasok sa sizing",
            sym, state.get("shelf_filing_count"), state.get("newest_filing_date"),
        )


def cached_shelf_state(symbol: str) -> dict[str, Any] | None:
    """CACHE-ONLY read para sa sizing seam — hindi kailanman nag-nenetwork."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    with _lock:
        hit = _state_cache.get(sym)
    if hit is None:
        return None
    fetched_at, state = hit
    if (time.monotonic() - fetched_at) >= _CACHE_TTL_S:
        return None
    return state


_FRESH_BUCKET_MAX_DAYS = 7
_ACTIVE_BUCKET_MAX_DAYS = 60


def shelf_age_bucket(days_since_newest: int | None, *, has_filing: bool) -> str:
    """Telemetry label ONLY (2026-09-02) — nothing reads it to size or gate.
    fresh (<= 7 d) | active (8-60 d) | stale (> 60 d) | unknown (a filing whose
    date is unparseable) | none (no shelf filing inside the lookback).
    Evidence for WHY it is only a label: C67 bucket mean R is monotonic (fresh
    -1.30 n=5, active -0.54 n=10, stale -0.13 n=43) but the fresh half is n=5
    with ONE out-of-sample symbol (VTAK) and D21's <= 10 d split points the
    other way (-1.72 with vs -1.61 without); the > 60 d up-size counterfactual
    is -$1,737 (W$ +1,365 vs L$ -6,577). Measure four weeks, then decide."""
    if not has_filing:
        return "none"
    if days_since_newest is None:
        return "unknown"
    if days_since_newest <= _FRESH_BUCKET_MAX_DAYS:
        return "fresh"
    if days_since_newest <= _ACTIVE_BUCKET_MAX_DAYS:
        return "active"
    return "stale"


def shelf_state_telemetry(
    state: dict[str, Any] | None,
    *,
    fraction: float,
    now_utc: Any = None,
) -> dict[str, Any] | None:
    """Pure telemetry record for ANY cached EDGAR state (2026-09-02).

    Returned whenever ``state`` is a dict (active OR inactive) — the damper's
    own ``shelf_damper_multiplier`` returns telemetry only when it FIRES, which
    left no durable trace for the 14/67 fills it did not damp nor for
    candidates that never filled. ``mult`` here is exactly what the damper
    applies (``fraction`` when active and valid, else 1.0) — recorded, not
    decided. ``days_since_newest`` is against the UTC calendar date of
    ``now_utc`` (default: wall clock; the runner passes the tick clock).
    None state ⇒ None (nothing to record)."""
    if not isinstance(state, dict):
        return None
    from datetime import datetime, timezone

    mult, _ = shelf_damper_multiplier(state, fraction=fraction)
    try:
        count = int(state.get("shelf_filing_count") or 0)
    except (TypeError, ValueError):
        count = 0
    newest = state.get("newest_filing_date")
    has_filing = bool(newest) or count > 0
    days: int | None = None
    if newest:
        try:
            _newest_d = datetime.strptime(str(newest), "%Y-%m-%d").date()
            if isinstance(now_utc, datetime):
                _now = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
                _today = _now.astimezone(timezone.utc).date()
            else:
                _today = datetime.now(timezone.utc).date()
            days = int((_today - _newest_d).days)
        except (TypeError, ValueError):
            days = None
    return {
        "mult": round(float(mult), 4),
        "shelf_active": state.get("shelf_active") is True,
        "shelf_filing_count": count,
        "newest_filing_date": newest,
        "newest_form": state.get("newest_form"),
        "days_since_newest": days,
        "age_bucket": shelf_age_bucket(days, has_filing=has_filing),
        "fetched_at": state.get("fetched_at"),
    }


def shelf_damper_multiplier(
    state: dict[str, Any] | None, *, fraction: float
) -> tuple[float, dict[str, Any] | None]:
    """Pure: (multiplier, telemetry). Positibong ebidensya lang ang nagpapababa;
    None/inactive/invalid fraction ⇒ 1.0 (fail-open). BYTE-IDENTICAL multiplier
    since 2026-08-18; the fired-case telemetry gained ``newest_form`` /
    ``fetched_at`` (2026-09-02) — use ``shelf_state_telemetry`` for the
    always-recorded form."""
    try:
        f = float(fraction)
    except (TypeError, ValueError):
        return 1.0, None
    if not (0.0 < f < 1.0):
        return 1.0, None
    if not isinstance(state, dict) or state.get("shelf_active") is not True:
        return 1.0, None
    return f, {
        "mult": round(f, 4),
        "shelf_filing_count": state.get("shelf_filing_count"),
        "newest_filing_date": state.get("newest_filing_date"),
        "newest_form": state.get("newest_form"),
        "fetched_at": state.get("fetched_at"),
    }


def reset_shelf_caches_for_tests() -> None:
    global _ticker_map, _ticker_map_at
    with _lock:
        _ticker_map = None
        _ticker_map_at = 0.0
        _state_cache.clear()
