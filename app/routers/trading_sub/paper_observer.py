"""Captured-paper PAPER lane observer (read-only).

2026-08-02: ang buhay na captured-paper Alpaca PAPER service (host process,
labas sa web container) ay INVISIBLE sa Live UI — walang endpoint na
nagpapakita ng lane state, kaya noong 07-29 ang "patay" na basa ng UI ay
nagpabulaan sa isang buhay na serbisyo, at ngayon (08-02) ang buhay na
serbisyo ay hindi makita ng operator. Ang observer na ito ay PURONG SELECT sa
mga table na sinusulatan ng service (heartbeat, selection frontier, route
states, order outbox) — walang anumang mutation, walang order authority.

Bawat query block ay may sariling maikling statement_timeout at fail-soft na
null/None sa error — ang isang mabigat/nakabarang table ay HINDI kailanman
magha-hang ng endpoint na ito (ang mismong sakit ng momentum desk endpoints).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx

router = APIRouter(tags=["trading-paper-observer"])
_log = logging.getLogger(__name__)

_STATEMENT_TIMEOUT_MS = 2000


def _bounded_row(db: Session, sql: str) -> tuple | None:
    """Isang row, may sariling statement_timeout, fail-soft None sa anumang error."""
    try:
        db.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'"))
        return db.execute(text(sql)).fetchone()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _age_s(ts: Any, now: datetime) -> float | None:
    try:
        if ts is None:
            return None
        t = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        return round((now - t).total_seconds(), 1)
    except Exception:
        return None


@router.get("/api/trading/observer/captured-paper")
def api_trading_observer_captured_paper(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {"ok": True, "generated_at": now.isoformat()}

    # Isang row kada beat; UUID ang id kaya WALANG kabuluhan ang ORDER BY id —
    # started_at ang beat time. Time-bounded sa 1h para hindi mag-scan nang
    # malalim: kapag patay ang service nang >1h, null ⇒ tapat na "WALANG
    # HEARTBEAT" sa strip.
    hb = _bounded_row(db, (
        "SELECT started_at, meta_json FROM brain_batch_jobs "
        "WHERE job_type = 'momentum_live_loop_heartbeat' "
        "AND started_at > now() - interval '1 hour' "
        "ORDER BY started_at DESC LIMIT 1"
    ))
    meta = (hb[1] if hb and isinstance(hb[1], dict) else {}) or {}
    out["service"] = {
        "heartbeat_at": hb[0].isoformat() if hb and hb[0] else None,
        "heartbeat_age_s": _age_s(hb[0] if hb else None, now),
        "account_scope": meta.get("account_scope"),
        "expected_account_id": meta.get("expected_account_id"),
        "live_cash_authorized": meta.get("live_cash_authorized"),
        "execution_family": meta.get("execution_family"),
        "runtime_generation": meta.get("runtime_generation"),
    }

    fr = _bounded_row(db, (
        "SELECT activation_generation, status, gap_count, last_source_event_at, updated_at "
        "FROM captured_paper_selection_frontiers ORDER BY updated_at DESC LIMIT 1"
    ))
    out["frontier"] = {
        "activation_generation": fr[0] if fr else None,
        "status": fr[1] if fr else None,
        "gap_count": fr[2] if fr else None,
        "last_source_event_at": fr[3].isoformat() if fr and fr[3] else None,
        "updated_age_s": _age_s(fr[4] if fr else None, now),
    }

    rs = _bounded_row(db, (
        "SELECT count(*) FILTER (WHERE updated_at > now() - interval '30 minutes'), "
        "count(*) FILTER (WHERE state = 'eligible') "
        "FROM captured_paper_selection_route_states"
    ))
    out["selection"] = {
        "route_states_updated_30m": rs[0] if rs else None,
        "eligible_count": rs[1] if rs else None,
    }

    ob = _bounded_row(db, (
        "SELECT count(*), count(*) FILTER (WHERE created_at > now() - interval '24 hours'), "
        "max(created_at) FROM captured_paper_post_commit_outbox"
    ))
    out["orders"] = {
        "outbox_total": ob[0] if ob else None,
        "outbox_24h": ob[1] if ob else None,
        "last_order_at": ob[2].isoformat() if ob and ob[2] else None,
    }

    # Tape recency: time-bounded window (walang index sa observed_at-only kaya
    # ang unbounded max() ay seq-scan — ang statement_timeout ang bakod; sa
    # weekend/patay na tape ito ay null nang tahimik).
    tp = _bounded_row(db, (
        "SELECT max(observed_at) FROM iqfeed_trade_ticks "
        "WHERE observed_at > now() - interval '2 hours'"
    ))
    out["tape"] = {
        "last_tick_at": tp[0].isoformat() if tp and tp[0] else None,
        "age_s": _age_s(tp[0] if tp else None, now),
        "note": None if (tp and tp[0]) else "walang tick sa huling 2h (sarado ang market o patay ang bridge)",
    }

    return JSONResponse(out)
