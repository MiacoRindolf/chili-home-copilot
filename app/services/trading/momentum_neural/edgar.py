"""SEC EDGAR dilution-risk client for the Ross momentum lane (gap #16).

Ross's CTNT-vs-SNTI lesson (videos 06/36): a cash-poor low-float that just FUNDED a deal
will issue shares and FADE despite good news. A recent registration / offering filing
(S-1, 424B*) is the tell. This module flags tickers with a recent dilution filing so
viability PENALIZES them. FREE feed (data.sec.gov); fail-open everywhere so a missing /
slow SEC response never blocks the lane. SEC requires a descriptive User-Agent + ~10 req/s.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_UA = "CHILI momentum research rindolf.miaco@gmail.com"
# Forms that signal ACTUAL / imminent share issuance (real dilution). A bare S-3 shelf is
# only a registration (no immediate issuance) -> excluded to avoid false positives; the
# 424B* takedowns + the S-1 follow-on are the real offerings that fade a low-float pump.
_DILUTION_FORMS = frozenset({"S-1", "S-1/A", "424B1", "424B2", "424B3", "424B4", "424B5"})
_DILUTION_MAX_AGE_DAYS = 60

_cik_map: dict[str, str] | None = None
_cik_map_at: float = 0.0
_dilution_cache: dict[str, tuple[float, bool]] = {}   # ticker -> (ts, is_diluter)
_CIK_TTL = 86400.0        # 1 day — the ticker->CIK map is near-static
_DILUTION_TTL = 10800.0   # 3 hours — intraday new-filing churn is low


def _recent_dilution_in(forms: list, dates: list, cutoff_iso: str) -> bool:
    """Pure: True when any (form, filingDate) pair is a dilution form filed on/after
    ``cutoff_iso`` (ISO date string compare — EDGAR dates are zero-padded YYYY-MM-DD)."""
    for f, d in zip(forms or [], dates or []):
        if str(f).strip() in _DILUTION_FORMS and str(d) >= cutoff_iso:
            return True
    return False


def _cik_for(ticker: str) -> str | None:
    global _cik_map, _cik_map_at
    now = time.time()
    if _cik_map is None or now - _cik_map_at > _CIK_TTL:
        try:
            import requests

            r = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": _UA}, timeout=10,
            )
            data = r.json()
            _cik_map = {
                str(v["ticker"]).upper(): f"{int(v['cik_str']):010d}" for v in data.values()
            }
            _cik_map_at = now
        except Exception:
            logger.debug("[edgar] cik map fetch failed", exc_info=True)
            if _cik_map is None:
                _cik_map = {}
    return _cik_map.get(str(ticker or "").upper())


def _has_recent_dilution(cik: str, *, max_age_days: int) -> bool:
    try:
        import requests
        from datetime import date, timedelta

        r = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": _UA}, timeout=10,
        )
        rec = (r.json().get("filings") or {}).get("recent") or {}
        cutoff = (date.today() - timedelta(days=int(max_age_days))).isoformat()
        return _recent_dilution_in(rec.get("form") or [], rec.get("filingDate") or [], cutoff)
    except Exception:
        logger.debug("[edgar] submissions fetch failed cik=%s", cik, exc_info=True)
        return False


def dilution_risk_symbols(
    tickers, *, max_age_days: int = _DILUTION_MAX_AGE_DAYS, max_lookups: int = 40
) -> set[str]:
    """Set of (upper) tickers with a recent dilution filing (S-1 / 424B* within
    ``max_age_days``). Per-ticker cached (3h); bounded to ``max_lookups`` NEW fetches per
    call so a cold start can't flood the SEC rate limiter (the rest warm next pass).
    Equity-only (crypto skipped). Fail-open to empty (no penalty) on any error."""
    out: set[str] = set()
    looked = 0
    now = time.time()
    for t in (tickers or []):
        tk = str(t or "").upper().strip()
        if not tk or tk.endswith("-USD"):
            continue
        cached = _dilution_cache.get(tk)
        if cached and now - cached[0] < _DILUTION_TTL:
            if cached[1]:
                out.add(tk)
            continue
        if looked >= max_lookups:
            continue   # warm the remainder on the next pass
        cik = _cik_for(tk)
        is_d = bool(cik) and _has_recent_dilution(cik, max_age_days=max_age_days)
        _dilution_cache[tk] = (now, is_d)
        looked += 1
        if is_d:
            out.add(tk)
    return out
