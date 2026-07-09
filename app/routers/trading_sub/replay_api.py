"""Replay API for the web research surface.

`live_fsm` runs the Replay v3 day runner against actual live sessions. The
legacy v2 tape replay remains available for as-of counterfactual research.
Both engines are backgrounded because day runs can take minutes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/momentum/replay", tags=["trading-replay"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# single-flight job state (module-level; one replay at a time because engines are heavy)
_lock = threading.Lock()
_job: dict[str, Any] = {
    "state": "idle",
    "date": None,
    "armed_source": None,
    "engine": None,
    "started_at": None,
    "error": None,
}


def _result_dir() -> str:
    from ...services.trading.momentum_neural import replay_v2

    return str(replay_v2.REPLAY_RESULTS_DIR)


def _v3_day_path(date: str) -> str:
    return os.path.join(_result_dir(), f"{date}_v3day.json")


def _psycopg2_database_url(url: str) -> str:
    clean = str(url or "")
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://"):
        if clean.startswith(prefix):
            return "postgresql://" + clean[len(prefix):]
    return clean


def _time_hhmm(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        text = str(raw)
        m = re.search(r"(\d{1,2}:\d{2})", text)
        return m.group(1) if m else None
    return dt.strftime("%H:%M")


def _float_or_none(raw: Any) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val == val else None


def _v3_trade_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in report.get("per_trade") or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "sym": str(item.get("symbol") or "").upper(),
            "session_id": item.get("session_id"),
            "t": _time_hhmm(item.get("recorded_entry_ts")),
            "exit_t": _time_hhmm(item.get("recorded_exit_ts")),
            "entry": item.get("recorded_entry"),
            "exit": item.get("recorded_exit"),
            "usd": item.get("recorded_pnl_usd"),
            "why": "Replay v3 recorded live trade",
            "trace_matches": bool(item.get("trace_matches")),
        })
    return rows


def _v3_trace_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in report.get("per_trade") or []:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").upper()
        sid = item.get("session_id")
        entry_t = _time_hhmm(item.get("recorded_entry_ts"))
        exit_t = _time_hhmm(item.get("recorded_exit_ts"))
        if entry_t:
            rows.append({"t": entry_t, "sym": sym, "stage": f"v3 live entry session {sid}"})
        if exit_t:
            pnl = _float_or_none(item.get("recorded_pnl_usd"))
            pnl_s = f"{pnl:+.2f}" if pnl is not None else ""
            rows.append({"t": exit_t, "sym": sym, "stage": f"v3 live exit {pnl_s}".strip()})
        if not bool(item.get("trace_matches")):
            rows.append({"t": entry_t or exit_t or "", "sym": sym, "stage": "v3 trace mismatch"})
    return rows


def _summarize_v3_day(report: dict[str, Any]) -> dict[str, Any]:
    out = dict(report or {})
    trades = _v3_trade_rows(out)
    out.update({
        "engine": "v3_day",
        "armed_source": "live_fsm",
        "total_usd": out.get("recorded_day_pnl_usd"),
        "trades": trades,
        "wins": sum(1 for t in trades if float(t.get("usd") or 0) > 0),
        "losses": sum(1 for t in trades if float(t.get("usd") or 0) < 0),
        "tape_symbols": out.get("armed_symbol_count"),
        "candidates": out.get("traded_session_count"),
        "halt_windows": None,
        "day_halted": None,
        "error": None,
        "ran_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return out


def _persist_v3_day(report: dict[str, Any]) -> dict[str, Any]:
    result = _summarize_v3_day(report)
    os.makedirs(_result_dir(), exist_ok=True)
    with open(_v3_day_path(str(result["date"])), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    return result


def _load_v3_day_stored(date: str) -> dict[str, Any] | None:
    try:
        with open(_v3_day_path(date), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("[replay_api] could not load Replay v3 result for %s", date, exc_info=True)
        return None


def _load_v3_day_result(date: str) -> dict[str, Any] | None:
    stored = _load_v3_day_stored(date)
    if stored is None:
        return None

    from ...services.trading.momentum_neural.replay_v2 import load_result as load_v2_result

    base = load_v2_result(date, armed_source="live") or {}
    result = {
        "date": date,
        "engine": "v3_day",
        "armed_source": "live_fsm",
        "entry_interval": base.get("entry_interval"),
        "bar_interval_min": base.get("bar_interval_min"),
        "ran_at_utc": stored.get("ran_at_utc"),
        "total_usd": stored.get("recorded_day_pnl_usd", stored.get("total_usd")),
        "wins": stored.get("wins"),
        "losses": stored.get("losses"),
        "trades": base.get("trades") or stored.get("trades") or [],
        "series": base.get("series") or {},
        "armed_timeline": base.get("armed_timeline") or [],
        "decision_trace": list(base.get("decision_trace") or []) + _v3_trace_rows(stored),
        "divergence": base.get("divergence") or [],
        "tape_symbols": stored.get("armed_symbol_count", base.get("tape_symbols")),
        "candidates": stored.get("traded_session_count", base.get("candidates")),
        "halt_windows": base.get("halt_windows"),
        "day_halted": base.get("day_halted"),
        "error": stored.get("error"),
        "replay_v3_day": stored,
        "chart_data_source": "replay_v2_live_tape" if base else "v3_summary_only",
    }
    if not result["trades"]:
        result["trades"] = stored.get("trades") or []
    return result


def _run_v2_in_thread(date: str, armed_source: str) -> None:
    global _job
    try:
        from ...services.trading.momentum_neural.replay_v2 import run_replay

        result = run_replay(date, armed_source=armed_source)
        with _lock:
            _job = {
                "state": "done" if not result.get("error") else "error",
                "date": date,
                "armed_source": armed_source,
                "engine": "v2",
                "started_at": _job.get("started_at"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": result.get("error"),
                "total_usd": result.get("total_usd"),
                "n_trades": len(result.get("trades") or []),
            }
    except Exception as exc:
        logger.warning("[replay_api] v2 run failed for %s", date, exc_info=True)
        with _lock:
            _job = {
                "state": "error",
                "date": date,
                "armed_source": armed_source,
                "engine": "v2",
                "started_at": _job.get("started_at"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:300],
            }


def _run_v3_day_in_thread(date: str) -> None:
    global _job
    try:
        from ...config import settings
        from scripts.replay_v3_day import run_day

        report = run_day(
            date,
            symbols=None,
            database_url=_psycopg2_database_url(settings.database_url),
        )
        result = _persist_v3_day(report)
        with _lock:
            _job = {
                "state": "done" if not result.get("error") else "error",
                "date": date,
                "armed_source": "live_fsm",
                "engine": "v3_day",
                "started_at": _job.get("started_at"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": result.get("error"),
                "total_usd": result.get("total_usd"),
                "n_trades": len(result.get("trades") or []),
            }
    except Exception as exc:
        logger.warning("[replay_api] Replay v3 day run failed for %s", date, exc_info=True)
        with _lock:
            _job = {
                "state": "error",
                "date": date,
                "armed_source": "live_fsm",
                "engine": "v3_day",
                "started_at": _job.get("started_at"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:300],
            }


@router.post("/run")
def run_replay_endpoint(payload: dict):
    date = str((payload or {}).get("date") or "").strip()
    armed_source = str((payload or {}).get("armed_source") or "live_fsm").strip()
    if armed_source not in ("asof", "live", "live_fsm"):
        raise HTTPException(status_code=400, detail="armed_source must be asof|live|live_fsm")
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    with _lock:
        if _job.get("state") == "running":
            return {"ok": False, "error": "replay_already_running", "job": _job}
        _job.clear()
        _job.update({
            "state": "running",
            "date": date,
            "armed_source": armed_source,
            "engine": "v3_day" if armed_source == "live_fsm" else "v2",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        })
    if armed_source == "live_fsm":
        t = threading.Thread(target=_run_v3_day_in_thread, args=(date,), name=f"replay-v3-day-{date}", daemon=True)
    else:
        t = threading.Thread(target=_run_v2_in_thread, args=(date, armed_source), name=f"replay-v2-{date}", daemon=True)
    t.start()
    return {"ok": True, "job": dict(_job)}


@router.get("/status")
def replay_status():
    with _lock:
        return {"ok": True, "job": dict(_job)}


@router.get("/list")
def replay_list():
    from ...services.trading.momentum_neural.replay_v2 import list_results

    return {"ok": True, "results": list_results()}


@router.get("/result/{date}")
def replay_result(date: str, armed_source: str = "live_fsm"):
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if armed_source == "live_fsm":
        result = _load_v3_day_result(date)
        if result is None:
            raise HTTPException(status_code=404, detail="no Replay v3 result for this date - run it first")
        return {"ok": True, "result": result}

    from ...services.trading.momentum_neural.replay_v2 import load_result

    result = load_result(date, armed_source=armed_source)
    if result is None:
        raise HTTPException(status_code=404, detail="no result for this date - run it first")
    return {"ok": True, "result": result}
