"""Shared nomination RECEIPT writer para sa lahat ng ignition source.

ANG PUWANG ([61], 2026-09-10). Ang ``momentum_ignition_nominations`` (mig 376) ay
may IISANG prodyuser: ang IQFeed ignition NOTIFY sa ``live_runner_loop``. Ang
bridge detector na nagpapadala ng NOTIFY na iyon ay tumatakbo LAMANG sa mga
pangalang naka-subscribe na — kaya para sa isang pangalang hindi pa nakikita ng
roster, ang UNANG spike ay imposibleng ma-nominate. Sinukat: 0 hilera sa table sa
23:45Z 09-10, habang sa 37 symbol-day ng aktuwal na trade ay nauna ang unang fire
sa unang tick natin ng p50 1.9 min / p75 3.7 min.

Ang pag-aayos ay nangangailangan ng PANGALAWANG prodyuser (ang snapshot
cross-section onset sa ``ignition_loop``), at ang dalawang prodyuser ay dapat
magsulat sa PAREHONG table — doon lang nila masusukat nang magkatabi ang latency
nila. Kaya inilipat dito ang writer: pure na param binder + isang SAVEPOINT-safe
na INSERT + isang short-lived-session na wrapper. Ang ``LiveRunnerLoop`` ay
nananatiling may sariling static method na tumatawag dito (walang pagbabago sa
gawi nito), at ang ignition loop ay tumatawag sa ``record_snapshot_onset``.

KONTRATA NG COLUMN (mig 376 + 377):
  * ``pct_change_60s`` ay FRACTION at EKSAKTONG 60 segundo ang sukat. Ang
    snapshot-onset na rise ay sinusukat sa velocity WINDOW (~180 s), kaya ang
    column na iyon ay iniiwang NULL para sa source na iyon at ang %rise ay nasa
    ``receipt.rise_pct`` kasama ang ``receipt.window_seconds``. Ang isang
    180-segundong halaga sa isang 60-segundong column ay hindi "malapit na" —
    mali ito, at ang derive script ay nagpe-percentile dito.
  * ``dollar_vol_60s`` ay tapat para sa dalawa: ang minute bar ng snapshot ay
    eksaktong 60 s na turnover (``min.v`` x ``min.vw``).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import text as _sql

_log = logging.getLogger(__name__)

# Sino ang nag-nominate. Ang default ng column ay `iqfeed_ignition`, kaya ang
# dating landas ay may parehong kahulugan bago at pagkatapos ng mig 377.
SOURCE_IQFEED_IGNITION = "iqfeed_ignition"
SOURCE_SNAPSHOT_ONSET = "snapshot_onset"

_INSERT_SQL = (
    "INSERT INTO momentum_ignition_nominations ("
    "symbol, fired_at, received_at, last_price, "
    "pct_change_60s, dollar_vol_60s, prints_10s, outcome, "
    "skipped, ross_universe_reason, source, cycle_index, receipt) VALUES ("
    ":symbol, :fired_at, :received_at, :last_price, "
    ":pct_change_60s, :dollar_vol_60s, :prints_10s, :outcome, "
    ":skipped, :ross_universe_reason, :source, :cycle_index, "
    "CAST(:receipt AS JSONB))"
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def ignition_nomination_params(
    data: dict,
    *,
    received_at: datetime,
    outcome: str,
    result: dict | None = None,
    source: str = SOURCE_IQFEED_IGNITION,
    cycle_index: int | None = None,
    receipt: dict | None = None,
    fired_at_parser: Callable[[Any], Any] | None = None,
) -> dict:
    """Bind one nomination row's parameters. Pure — no I/O, no clock.

    ``fired_at_parser`` lets the IQFeed caller keep its own strict aware-UTC
    parser for the producer's string timestamp; a caller that already holds a
    datetime passes none.
    """
    prints_10s = data.get("prints_10s")
    outcome_result = result if isinstance(result, dict) else {}
    fired_at = data.get("fired_at")
    if fired_at_parser is not None:
        fired_at = fired_at_parser(fired_at)
    return {
        "symbol": str(data.get("symbol") or "")[:16],
        "fired_at": fired_at,
        "received_at": received_at,
        "last_price": _num(data.get("last_price")),
        # FRACTION, exactly as the producer reports it (0.05 == +5%).
        "pct_change_60s": _num(data.get("pct_change_60s")),
        # SIXTY-SECOND turnover; never the day-scale tradability number.
        "dollar_vol_60s": _num(data.get("dollar_vol_60s")),
        "prints_10s": (
            int(prints_10s)
            if isinstance(prints_10s, int) and not isinstance(prints_10s, bool)
            else None
        ),
        "outcome": _text(outcome, 48) or "unknown",
        "skipped": _text(outcome_result.get("skipped"), 64),
        "ross_universe_reason": _text(
            outcome_result.get("ross_universe_reason"), 64
        ),
        "source": _text(source, 32) or SOURCE_IQFEED_IGNITION,
        "cycle_index": (
            int(cycle_index)
            if isinstance(cycle_index, int) and not isinstance(cycle_index, bool)
            else None
        ),
        "receipt": (
            json.dumps(receipt, default=str, sort_keys=True)
            if isinstance(receipt, dict)
            else None
        ),
    }


def write_ignition_nomination(db: Any, params: dict) -> bool:
    """INSERT one nomination row on the CALLER's session/transaction.

    Runs inside a SAVEPOINT so a failed observation (missing table on a
    pre-migration-376/377 environment, a constraint, a transient error) can never
    abort the transaction it shares — the same contract
    ``bridge_subscribe.request_bridge_subscription`` uses, and for the same
    reason: an observation must never break the path it observes.
    """
    try:
        with db.begin_nested():
            db.execute(_sql(_INSERT_SQL), params)
        return True
    except Exception:
        _log.debug(
            "[ignition_receipts] nomination record failed symbol=%s source=%s",
            params.get("symbol"),
            params.get("source"),
            exc_info=True,
        )
        return False


def record_ignition_nomination(
    params: dict,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Write one nomination row on a SHORT-LIVED session of its own. Never raises."""
    if session_factory is None:
        from ....db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    db = None
    try:
        db = session_factory()
        written = write_ignition_nomination(db, params)
        db.commit()
        return written
    except Exception:
        _log.debug(
            "[ignition_receipts] nomination session failed symbol=%s",
            params.get("symbol"),
            exc_info=True,
        )
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def record_snapshot_onset(
    onset: dict,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> dict:
    """Receipt ONE snapshot-onset admission and hint the tape at the same moment.

    Ang dalawang epekto ay sinasadyang MAGKASAMA sa iisang transaksyon: ang hilera
    ang ebidensya na nakita natin ang onset, at ang subscribe hint ang tanging
    dahilan kung bakit magkakaroon tayo ng tape para patunayan ang susunod na
    spike. Kapag nag-commit ang isa nang wala ang isa, sinungaling ang libro.

    Ibinabalik ang ``{"recorded": bool, "subscribed": bool}``. Hindi kailanman
    nagre-raise: tapos na ang admission bago pa ito tawagin.
    """
    from .bridge_subscribe import request_bridge_subscription

    if session_factory is None:
        from ....db import SessionLocal as _SessionLocal

        session_factory = _SessionLocal
    symbol = str(onset.get("symbol") or "").strip().upper()
    out = {"recorded": False, "subscribed": False}
    if not symbol:
        return out
    db = None
    try:
        db = session_factory()
        params = ignition_nomination_params(
            onset,
            received_at=onset.get("received_at") or onset.get("fired_at"),
            outcome=str(onset.get("outcome") or "snapshot_onset_admitted"),
            source=SOURCE_SNAPSHOT_ONSET,
            cycle_index=onset.get("cycle_index"),
            receipt=onset.get("receipt"),
        )
        out["recorded"] = write_ignition_nomination(db, params)
        out["subscribed"] = bool(
            request_bridge_subscription(db, symbol, reason=SOURCE_SNAPSHOT_ONSET)
        )
        db.commit()
        return out
    except Exception:
        _log.debug(
            "[ignition_receipts] snapshot onset receipt failed symbol=%s",
            symbol,
            exc_info=True,
        )
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        return out
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
