"""Ross stream transcript -> equity momentum viability bridge.

Ross mentioning or trying a ticker is discovery, not an order instruction. This
module turns very recent transcript rows into an immediate Ross-lane viability
refresh with live market evidence attached. The normal auto-arm, risk, venue,
and tick-entry gates still decide whether anything is watched or traded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from .universe import (
    EQUITY_ROSS_SMALLCAP,
    _pos_in_range,
    _snapshot_adv_shares,
    _snapshot_price,
    _snapshot_today_shares,
    _snapshot_volume_pace,
    build_equity_universe,
)

logger = logging.getLogger(__name__)

DEFAULT_TRANSCRIPT_PATH = r"D:\CHILI-Docker\chili-data\ross_stream\transcript.jsonl"
DEFAULT_WARRIOR_SESSION_OK_PATH = r"D:\CHILI-Docker\chili-data\ross_stream\warrior_session_ok.json"

_WORD_SYMBOL_RE = re.compile(r"\b[A-Z]{2,5}\b")
_DASHED_SYMBOL_RE = re.compile(r"\b(?:[A-Z]\s*-\s*){1,4}[A-Z]\b")
_STRONG_TRADING_CONTEXT_RE = re.compile(
    r"\b("
    r"add|ask|bid|break(?:ing|out)?|candle|chart|curl|daily|entry|exit|"
    r"float|gap(?:per)?|halt|high(?:\s+of\s+day|\s+day)?|hod|level|"
    r"low|lod|momentum|offer|premarket|price|pullback|"
    r"reclaim|red|resistance|risk|runner|scalp|scanner|share[s]?|"
    r"starter|stock|stop|support|ticker|trade|trading|volume|vwap"
    r")",
    re.IGNORECASE,
)
_SOFT_TRADING_CONTEXT_RE = re.compile(
    r"\b(long|watch(?:ing)?|interested(?:\s+in)?|interesting(?:\s+to\s+me)?)\b",
    re.IGNORECASE,
)
_OVER_MARKET_CONTEXT_RE = re.compile(
    r"\bover\s+(?:\$?\d+(?:\.\d+)?|vwap|high|hod|pre[-\s]?market\s+high)\b",
    re.IGNORECASE,
)
_NON_TRADING_CONTEXT_RE = re.compile(
    r"\b("
    r"long\s+conversation|watch(?:ing)?\s+(?:our\s+)?(?:show|video|episode|movie|tv|clip|tiktok)|"
    r"qr\s+code|"
    r"noise\s+like\s+[A-Z]{2,5}!|"
    r"that\s+was\s+amazing|"
    r"\butopian\b|"
    r"\bmcdonald'?s\b|"
    r"\bwalmart\b|"
    r"\bfentanyl\b"
    r")\b",
    re.IGNORECASE,
)
_RECAP_CONTEXT_RE = re.compile(
    r"\b("
    r"(?:this|that)\s+was\s+(?:the\s+)?(?:first|second|third|stock|trade)|"
    r"(?:i|we)\s+(?:already\s+)?traded\b|"
    r"(?:i|we)\s+was\s+trading\b|"
    r"\bi\s+thought\s+(?:it\s+)?was\b|"
    r"\bwhich\s+i\s+thought\s+was\b|"
    r"\bwas\s+pretty\s+good\b|"
    r"\bhits?\s+(?:the\s+)?(?:running\s+up\s+)?scanner\s+at\s+\d{1,2}(?::?\d{2})?\b|"
    r"\bat\s+\d{1,2}[\.:]?\d{2}\s*(?:am|pm)?\s+when\b.{0,80}\bscanner\b|"
    r"(?:we(?:'ll| will)|i(?:'ll| will))\s+break\s+(?:this|that|it|one)\s+down|"
    r"break\s+(?:this|that|it|one)\s+down\s+in\s+a\s+second|"
    r"glad\s+to\s+see\s+it\s+moving\s+higher|"
    r"\bfrom\s+earlier\b|"
    r"\bfor\s+later\b|"
    r"\bold\s+headlines?\b|"
    r"\bwas\s+almost\s+immediately\s+up\b|"
    r"\bwere\s+a\s+little\s+early\s+on\b|"
    r"\bi\s+did\s+miss\b|"
    r"\bgood\s+example\s+of\b|"
    r"\bwhen\s+it\s+opened\b|"
    r"\bkind\s+of\.\.\."
    r")",
    re.IGNORECASE,
)
_AMBIGUOUS_TICKER_CORRECTION_RE = re.compile(
    r"\b("
    r"no\s+this\s+is|"
    r"what\s+was\s+it|"
    r"forgot\s+which\s+ticker|"
    r"which\s+ticker\s+(?:it|that)\s+was"
    r")\b",
    re.IGNORECASE,
)

_STOP_SYMBOLS = frozenset(
    {
        "AI",
        "AM",
        "API",
        "ASK",
        "BID",
        "CBS",
        "CEO",
        "CFO",
        "CTO",
        "DMA",
        "ECN",
        "EDT",
        "EMA",
        "EST",
        "ET",
        "FDA",
        "GDP",
        "HOD",
        "IPO",
        "LOD",
        "LLC",
        "LULD",
        "MACD",
        "NASDAQ",
        "NBBO",
        "NYSE",
        "ORB",
        "OTC",
        "PDT",
        "PM",
        "PST",
        "QR",
        "RTH",
        "SEC",
        "SIM",
        "SMA",
        "SSR",
        "US",
        "UTC",
        "VWAP",
    }
)


@dataclass(frozen=True)
class TranscriptMention:
    symbol: str
    ts: datetime
    text: str

    @property
    def key(self) -> str:
        digest = hashlib.sha1(self.text.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{self.ts.isoformat()}|{self.symbol}|{digest}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _finite_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _add_symbol(out: list[str], seen: set[str], raw: str) -> None:
    sym = re.sub(r"[^A-Z]", "", str(raw or "").upper())
    if not (2 <= len(sym) <= 5):
        return
    if sym in _STOP_SYMBOLS:
        return
    if sym not in seen:
        seen.add(sym)
        out.append(sym)


def extract_tickers_from_text(text_value: str) -> list[str]:
    """Extract likely US equity tickers from a Ross transcript snippet.

    Handles normal uppercase symbols (``JEM``) and spelled tickers
    (``C-A-N-F``). It deliberately avoids fuzzy lowercase recovery; guessing
    that "gem" means JEM is too risky for a live trading bridge.
    """
    text_s = str(text_value or "")
    if not text_s:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for match in _DASHED_SYMBOL_RE.finditer(text_s):
        _add_symbol(out, seen, match.group(0))
    for match in _WORD_SYMBOL_RE.finditer(text_s):
        _add_symbol(out, seen, match.group(0))
    return out


def has_trading_context(text_value: str) -> bool:
    """Return whether a transcript row sounds like trading, not random audio.

    The audio capture can keep transcribing after Ross is done or when another
    video is playing. A bare uppercase word such as "GP" is not enough evidence
    to feed a live trading discovery bridge.
    """
    text_s = str(text_value or "")
    if not text_s:
        return False
    if _NON_TRADING_CONTEXT_RE.search(text_s):
        return False
    if _RECAP_CONTEXT_RE.search(text_s):
        return False
    if _AMBIGUOUS_TICKER_CORRECTION_RE.search(text_s):
        return False
    if _STRONG_TRADING_CONTEXT_RE.search(text_s):
        return True
    if extract_tickers_from_text(text_s) and _OVER_MARKET_CONTEXT_RE.search(text_s):
        return True
    # Spelled symbols are uncommon outside trading discussion and are how Ross
    # often disambiguates noisy tickers on stream.
    if _DASHED_SYMBOL_RE.search(text_s):
        return True
    # "Long" and "watching" are valid Ross words, but also common English.
    # Only trust them when a valid uppercase ticker survived the stoplist.
    return bool(_SOFT_TRADING_CONTEXT_RE.search(text_s) and extract_tickers_from_text(text_s))


def _read_tail_lines(path: str | os.PathLike[str], *, max_lines: int) -> list[str]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return lines[-max(1, int(max_lines)) :]


def warrior_session_marker_ok(
    path: str | os.PathLike[str] | None = None,
    *,
    now_utc: datetime | None = None,
    max_age_seconds: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether transcript ingestion has a fresh live Warrior-session marker."""
    p = Path(
        path
        or getattr(settings, "chili_momentum_ross_transcript_warrior_session_ok_path", DEFAULT_WARRIOR_SESSION_OK_PATH)
        or DEFAULT_WARRIOR_SESSION_OK_PATH
    )
    now = (now_utc or _utc_now()).astimezone(timezone.utc)
    try:
        max_age = (
            float(max_age_seconds)
            if max_age_seconds is not None
            else float(getattr(settings, "chili_momentum_ross_transcript_warrior_session_ok_max_age_seconds", 30.0) or 30.0)
        )
    except (TypeError, ValueError):
        max_age = 30.0
    if not p.is_file():
        return False, "warrior_session_marker_missing", {"path": str(p)}
    try:
        row = json.loads(p.read_text(encoding="utf-8", errors="ignore") or "{}")
    except Exception as exc:
        return False, "warrior_session_marker_invalid", {"path": str(p), "error": str(exc)[:160]}
    if not isinstance(row, dict) or row.get("ok") is not True:
        reason = str(row.get("reason") or "").strip() if isinstance(row, dict) else ""
        if not reason or reason == "warrior_session_ok":
            reason = "warrior_session_marker_not_ok"
        return False, reason, {"path": str(p), "marker": row if isinstance(row, dict) else None}
    ts = _parse_ts(row.get("ts") or row.get("checked_at") or row.get("updated_at"))
    if ts is None:
        return False, "warrior_session_marker_missing_ts", {"path": str(p)}
    age_s = max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())
    details = {
        "path": str(p),
        "age_s": age_s,
        "max_age_s": max_age,
        "url": row.get("url"),
        "title": row.get("title"),
        "video_count": row.get("video_count"),
    }
    if age_s > max(1.0, max_age):
        return False, "warrior_session_marker_stale", details
    if int(row.get("video_count") or 0) <= 0 and not bool(row.get("stream_visible")):
        return False, "warrior_session_marker_no_stream", details
    return True, "warrior_session_ok", details


def recent_transcript_mentions(
    path: str | os.PathLike[str],
    *,
    now_utc: datetime | None = None,
    lookback_seconds: float = 90.0,
    max_lines: int = 400,
    max_symbols: int = 8,
) -> list[TranscriptMention]:
    """Return latest distinct ticker mentions from recent transcript rows."""
    now_utc = (now_utc or _utc_now()).astimezone(timezone.utc)
    cutoff = now_utc - timedelta(seconds=max(1.0, float(lookback_seconds)))
    latest: dict[str, TranscriptMention] = {}
    for line in _read_tail_lines(path, max_lines=max_lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < cutoff or ts > now_utc + timedelta(seconds=10):
            continue
        text_s = str(row.get("text") or "").strip()
        if not text_s:
            continue
        if not has_trading_context(text_s):
            continue
        for sym in extract_tickers_from_text(text_s):
            prev = latest.get(sym)
            mention = TranscriptMention(symbol=sym, ts=ts, text=text_s)
            if prev is None or mention.ts >= prev.ts:
                latest[sym] = mention
    mentions = sorted(latest.values(), key=lambda m: m.ts, reverse=True)
    return mentions[: max(1, int(max_symbols))]


def _snapshot_by_symbol(snapshot: Iterable[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in snapshot or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        if sym and sym not in out:
            out[sym] = row
    return out


def _likely_missing_leading_letter_alias(
    mentioned: str,
    candidate: str,
    *,
    text: str,
    snapshot_row: dict[str, Any] | None = None,
    tape_row: dict[str, Any] | None = None,
) -> bool:
    """Constrained ASR repair for clipped leading-letter tickers.

    Ross audio sometimes drops the first hard consonant ("CLRO" -> "LRO"). This
    is intentionally much narrower than fuzzy symbol matching: only one missing
    leading letter, only with a live Ross-style market row, and only on action
    language. Near-symbol guesses such as DXTS->DXF remain audit warnings.
    """
    raw = re.sub(r"[^A-Z]", "", str(mentioned or "").upper())
    cand = re.sub(r"[^A-Z]", "", str(candidate or "").upper())
    if not (3 <= len(raw) <= 4 and len(cand) == len(raw) + 1 and cand.endswith(raw)):
        return False
    text_s = str(text or "")
    if not re.search(
        r"\b(starter|squeeze|through|break(?:out|ing)?|flat\s*top|cup|handle|pull\s*back|pullback)\b",
        text_s,
        re.IGNORECASE,
    ):
        return False
    price = _snapshot_price(snapshot_row or {}) if snapshot_row else _finite_float((tape_row or {}).get("mid"))
    change_pct = _daily_change_pct(snapshot_row, price) if snapshot_row else None
    day_volume = _snapshot_today_shares(snapshot_row or {}) if snapshot_row else _finite_float((tape_row or {}).get("day_volume"))
    dollar_volume = (float(price) * float(day_volume)) if price and day_volume else None
    if price is None or not (1.0 <= float(price) <= 20.0):
        return False
    if snapshot_row is not None:
        return bool((change_pct is not None and float(change_pct) >= 5.0) or (dollar_volume and dollar_volume >= 1_000_000.0))
    return bool(dollar_volume and dollar_volume >= 1_000_000.0)


def _resolve_missing_leading_letter_mentions(
    mentions: list[TranscriptMention],
    *,
    snapshot_map: dict[str, dict[str, Any]],
    tape_rows: dict[str, dict[str, Any]],
) -> list[TranscriptMention]:
    if not mentions:
        return []
    available = sorted(set(snapshot_map) | {str(sym or "").upper() for sym in tape_rows})
    resolved: list[TranscriptMention] = []
    for mention in mentions:
        sym = str(mention.symbol or "").upper()
        if sym in snapshot_map or sym in tape_rows:
            resolved.append(mention)
            continue
        matches = [
            other
            for other in available
            if _likely_missing_leading_letter_alias(
                sym,
                other,
                text=mention.text,
                snapshot_row=snapshot_map.get(other),
                tape_row=tape_rows.get(other),
            )
        ]
        if len(matches) == 1:
            resolved.append(TranscriptMention(symbol=matches[0], ts=mention.ts, text=mention.text))
        else:
            resolved.append(mention)
    return resolved


def _symbol_edit_distance(left: str, right: str) -> int:
    a = re.sub(r"[^A-Z]", "", str(left or "").upper())
    b = re.sub(r"[^A-Z]", "", str(right or "").upper())
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(
                min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = cur
    return prev[-1]


def symbol_resolution_warnings(
    mentioned_symbols: Iterable[str],
    *,
    resolved_signals: dict[str, dict[str, Any]],
    snapshot: Iterable[dict[str, Any]] | None = None,
    tape_rows: dict[str, dict[str, Any]] | None = None,
    max_distance: int = 2,
) -> list[dict[str, Any]]:
    """Report transcript symbols that were not resolved to exact market evidence.

    This is intentionally audit-only. A near symbol such as ``DXF`` when the
    transcript heard ``DXTS`` is not safe to auto-trade, but it must be visible
    so the Ross-vs-CHILI audit does not look like a scanner miss.
    """
    snapshot_map = _snapshot_by_symbol(snapshot)
    tape_rows = tape_rows or {}
    available = sorted(set(snapshot_map) | {str(sym or "").upper() for sym in tape_rows})
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in mentioned_symbols:
        sym = str(raw or "").strip().upper()
        if not sym or sym in seen or sym in resolved_signals:
            continue
        seen.add(sym)
        candidates: list[dict[str, Any]] = []
        for other in available:
            if not other or other == sym:
                continue
            if len(sym) >= 2 and len(other) >= 2 and sym[:2] != other[:2]:
                continue
            dist = _symbol_edit_distance(sym, other)
            if dist > max(0, int(max_distance)):
                continue
            row = snapshot_map.get(other) or {}
            tape = tape_rows.get(other) or {}
            candidates.append(
                {
                    "symbol": other,
                    "edit_distance": dist,
                    "price": _snapshot_price(row) if row else _finite_float(tape.get("mid")),
                    "change_pct": _daily_change_pct(row, _snapshot_price(row)) if row else None,
                    "source": "snapshot" if row else "tape",
                }
            )
        candidates.sort(key=lambda item: (int(item.get("edit_distance") or 99), str(item.get("symbol") or "")))
        if candidates:
            warnings.append(
                {
                    "mentioned_symbol": sym,
                    "reason": "mentioned_symbol_unresolved_near_market_symbol",
                    "near_symbols": candidates[:5],
                }
            )
    return warnings


def _latest_tape_rows(
    db: Session,
    symbols: list[str],
    *,
    now_utc: datetime,
    max_age_seconds: float,
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    since = now_utc.replace(tzinfo=None) - timedelta(seconds=max(1.0, float(max_age_seconds)))
    try:
        rows = db.execute(
            text(
                "SELECT DISTINCT ON (symbol) symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source "
                "FROM momentum_nbbo_spread_tape "
                "WHERE symbol = ANY(:symbols) AND observed_at >= :since "
                "ORDER BY symbol, observed_at DESC"
            ),
            {"symbols": symbols, "since": since},
        ).mappings().all()
    except Exception as exc:
        logger.debug("[ross_transcript_bridge] latest tape read failed: %s", exc)
        return {}
    return {str(r["symbol"]).upper(): dict(r) for r in rows if r.get("symbol")}


def _daily_change_pct(snapshot_row: dict[str, Any] | None, price: float | None) -> float | None:
    if not snapshot_row:
        return None
    chg = _finite_float(snapshot_row.get("todaysChangePerc"))
    if chg is not None:
        return chg
    prev = snapshot_row.get("prevDay") if isinstance(snapshot_row.get("prevDay"), dict) else {}
    prev_close = _finite_float(prev.get("c"))
    if price is not None and prev_close and prev_close > 0:
        return (float(price) - prev_close) / prev_close * 100.0
    return None


def _field_signal(
    symbol: str,
    *,
    snapshot_row: dict[str, Any] | None,
    tape_row: dict[str, Any] | None = None,
    mention: TranscriptMention | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    now_utc = now_utc or _utc_now()
    price = _snapshot_price(snapshot_row or {}) if snapshot_row else None
    if price is None and tape_row:
        price = _finite_float(tape_row.get("mid"))

    today_shares = _snapshot_today_shares(snapshot_row or {}) if snapshot_row else None
    if today_shares is None and tape_row:
        today_shares = _finite_float(tape_row.get("day_volume"))
    adv_shares = _snapshot_adv_shares(snapshot_row or {}) if snapshot_row else None
    pace = _snapshot_volume_pace(snapshot_row or {}, now=now_utc) if snapshot_row else {}
    change_pct = _daily_change_pct(snapshot_row, price)

    signal: dict[str, Any] = {
        "ticker": sym,
        "symbol": sym,
        "direction": "long",
        "source": "equity_snapshot_ross_field",
        "scanner_source": "equity_snapshot_ross_field",
        "signal_type": "ross_field_snapshot",
    }
    if mention is not None:
        signal.update(
            {
                "source": "ross_audio_transcript warrior ross 5 pillars",
                "scanner_source": "ross_audio_transcript",
                "signal_type": "ross_transcript_mention",
                "transcript_ts": mention.ts.isoformat(),
                "transcript_text": mention.text[:320],
                "playbook_hint": "ross_breakout_starter_or_first_pullback",
            }
        )
    if price is not None:
        signal["price"] = float(price)
        signal["last_price"] = float(price)
    if change_pct is not None:
        signal["daily_change_pct"] = float(change_pct)
        signal["change_pct"] = float(change_pct)
        signal["todays_change_perc"] = float(change_pct)
    if today_shares is not None:
        signal["volume"] = float(today_shares)
        signal["day_volume"] = float(today_shares)
    if adv_shares is not None:
        signal["prev_day_volume"] = float(adv_shares)
    if price is not None and today_shares is not None:
        signal["dollar_volume"] = float(price) * float(today_shares)
    if isinstance(pace, dict):
        for key in (
            "rvol_pace",
            "rvol_source",
            "rvol_basis",
            "expected_cum_vol",
            "actual_cum_vol",
            "session_elapsed_fraction",
            "session_bucket",
            "fallback_reason",
        ):
            if pace.get(key) is not None:
                signal[key] = pace[key]
        if pace.get("rvol_pace") is not None:
            signal["rvol"] = pace["rvol_pace"]
    if snapshot_row:
        signal["day_range_pos"] = _pos_in_range(snapshot_row, price)
    if tape_row:
        for key in ("bid", "ask", "mid", "spread_bps"):
            if tape_row.get(key) is not None:
                signal[key] = float(tape_row[key])
        if tape_row.get("source"):
            signal["l1_source"] = str(tape_row["source"])

    explosive = False
    try:
        explosive = (
            (change_pct is not None and float(change_pct) >= 10.0)
            or (signal.get("rvol_pace") is not None and float(signal["rvol_pace"]) >= 5.0)
        )
    except (TypeError, ValueError):
        explosive = False
    if explosive:
        signal["daily_breaking_major"] = True

    # Need at least one market pillar or price. A bare transcript mention is not
    # enough to put a symbol into live eligibility.
    if not any(signal.get(k) is not None for k in ("price", "daily_change_pct", "rvol_pace", "volume")):
        return None
    return signal


def build_ross_transcript_signal_map(
    mentions: list[TranscriptMention],
    *,
    snapshot: list[dict[str, Any]] | None,
    tape_rows: dict[str, dict[str, Any]] | None = None,
    now_utc: datetime | None = None,
    max_field_symbols: int = 50,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Build focus tickers plus Ross-field context for percentile scoring."""
    now_utc = now_utc or _utc_now()
    snapshot_map = _snapshot_by_symbol(snapshot)
    tape_rows = tape_rows or {}
    mentions = _resolve_missing_leading_letter_mentions(
        mentions,
        snapshot_map=snapshot_map,
        tape_rows=tape_rows,
    )
    focus: list[str] = []
    mention_by_symbol: dict[str, TranscriptMention] = {}
    for mention in mentions:
        if mention.symbol not in mention_by_symbol:
            mention_by_symbol[mention.symbol] = mention
            focus.append(mention.symbol)

    field_symbols: list[str] = []
    try:
        field_symbols = build_equity_universe(
            EQUITY_ROSS_SMALLCAP,
            snapshot=snapshot,
        )[: max(1, int(max_field_symbols))]
    except Exception:
        field_symbols = []

    ordered_symbols = focus + [s for s in field_symbols if s not in focus]
    signals: dict[str, dict[str, Any]] = {}
    for sym in ordered_symbols:
        sig = _field_signal(
            sym,
            snapshot_row=snapshot_map.get(sym),
            tape_row=tape_rows.get(sym),
            mention=mention_by_symbol.get(sym),
            now_utc=now_utc,
        )
        if sig is not None:
            signals[sym] = sig
    focus = [s for s in focus if s in signals]
    return focus, signals


def _settings_default_path() -> str:
    return str(
        os.environ.get("ROSS_TRANSCRIPT_PATH")
        or getattr(settings, "chili_momentum_ross_transcript_path", DEFAULT_TRANSCRIPT_PATH)
        or DEFAULT_TRANSCRIPT_PATH
    )


def run_ross_transcript_bridge_once(
    db: Session,
    *,
    transcript_path: str | None = None,
    now_utc: datetime | None = None,
    lookback_seconds: float | None = None,
    max_symbols: int | None = None,
    max_lines: int = 400,
    max_field_symbols: int = 50,
    processed_keys: set[str] | None = None,
    snapshot_provider: Callable[[], list[dict[str, Any]]] | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
    admitter: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Process recent transcript ticker mentions into fresh viability rows."""
    if not bool(getattr(settings, "chili_momentum_ross_transcript_bridge_enabled", True)):
        return {"ok": True, "skipped": "flag_off", "mentions": 0, "scored": 0}

    if bool(getattr(settings, "chili_momentum_ross_transcript_require_warrior_session_ok", True)):
        marker_ok, marker_reason, marker_detail = warrior_session_marker_ok(now_utc=now_utc)
        if not marker_ok:
            return {
                "ok": True,
                "skipped": marker_reason,
                "mentions": 0,
                "scored": 0,
                "warrior_session": marker_detail,
            }

    now_utc = now_utc or _utc_now()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    lookback = (
        float(lookback_seconds)
        if lookback_seconds is not None
        else float(getattr(settings, "chili_momentum_ross_transcript_bridge_lookback_seconds", 90.0) or 90.0)
    )
    max_syms = (
        int(max_symbols)
        if max_symbols is not None
        else int(getattr(settings, "chili_momentum_ross_transcript_bridge_max_symbols", 8) or 8)
    )
    path = transcript_path or _settings_default_path()
    mentions = recent_transcript_mentions(
        path,
        now_utc=now_utc,
        lookback_seconds=lookback,
        max_lines=max_lines,
        max_symbols=max_syms,
    )
    if processed_keys is not None:
        mentions = [m for m in mentions if m.key not in processed_keys]
    if not mentions:
        return {"ok": True, "skipped": "no_recent_mentions", "mentions": 0, "scored": 0}

    try:
        if snapshot_provider is not None:
            snapshot = snapshot_provider() or []
        else:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(max_age_seconds=30.0) or []
    except Exception as exc:
        logger.debug("[ross_transcript_bridge] snapshot fetch failed: %s", exc)
        snapshot = []

    raw_symbols = [m.symbol for m in mentions]
    tape_rows = _latest_tape_rows(
        db,
        raw_symbols,
        now_utc=now_utc,
        max_age_seconds=max(lookback, 120.0),
    )
    focus, ross_signals = build_ross_transcript_signal_map(
        mentions,
        snapshot=snapshot,
        tape_rows=tape_rows,
        now_utc=now_utc,
        max_field_symbols=max_field_symbols,
    )
    resolution_warnings = symbol_resolution_warnings(
        raw_symbols,
        resolved_signals=ross_signals,
        snapshot=snapshot,
        tape_rows=tape_rows,
    )
    if not focus:
        if processed_keys is not None:
            processed_keys.update(m.key for m in mentions)
        return {
            "ok": True,
            "skipped": "mentions_without_market_pillars",
            "mentions": len(mentions),
            "symbols": raw_symbols,
            "scored": 0,
            "symbol_resolution_warnings": resolution_warnings,
        }

    runner_was_injected = runner is not None
    if runner is None:
        from .pipeline import run_momentum_neural_tick as runner

    meta = {
        "tickers": focus,
        "ross_signals": ross_signals,
        "ross_transcript_bridge": True,
        "ross_transcript_bridge_ts": now_utc.isoformat(),
    }
    result = runner(db, meta=meta)
    admissions: list[dict[str, Any]] = []
    if bool(getattr(settings, "chili_momentum_ross_event_admission_enabled", True)):
        # Production uses the normal pipeline runner and then admits immediately.
        # Unit tests often inject a fake runner with an object() db; skip default
        # admission there unless the test supplied an admitter seam explicitly.
        if admitter is None and not runner_was_injected:
            from .ross_event_admission import admit_ross_event as admitter
        if admitter is not None:
            for sym in focus:
                try:
                    admissions.append(
                        admitter(
                            db,
                            symbol=sym,
                            signal=ross_signals.get(sym),
                            source="ross_transcript",
                            refresh_viability=False,
                        )
                    )
                except Exception as exc:
                    admissions.append(
                        {
                            "ok": False,
                            "symbol": sym,
                            "source": "ross_transcript",
                            "error": str(exc)[:200],
                        }
                    )
    if processed_keys is not None:
        processed_keys.update(m.key for m in mentions)
    return {
        "ok": True,
        "mentions": len(mentions),
        "symbols": focus,
        "field_symbols": len(ross_signals),
        "scored": len(focus),
        "symbol_resolution_warnings": resolution_warnings,
        "pipeline": result,
        "admitted": sum(1 for a in admissions if a.get("admitted")),
        "admissions": admissions,
    }
