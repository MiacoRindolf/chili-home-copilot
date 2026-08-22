"""Repeg spoof-wall detector (Ross "Forcing a Crash" 2026-08-21, court docs).

Ang MEMX-class suppression algo signature: malaking ask wall ~10-20c sa itaas
ng market na, kapag nilalapitan/kinakain, ay KINAKANSELA at inaakyat (sub-second
repeg) — phantom supply, hindi tunay na seller. Tatlong estado ang inuuri mula
sa magkakasunod na depth snapshot (iqfeed_depth_snapshots asks_json ladders) at
mga print (iqfeed_trade_ticks) sa iisang bounded window:

  SPOOF_WALL_ACTIVE — >=2 pataas na repeg event (nawala ang wall sa P nang
      WALANG sapat na prints doon, may kapareho-laki na wall sa P' > P):
      suppression na umaandar — iwas muna / ibang pangalan (Ross doctrine).
  ICEBERG_REAL — ang wall ay NANANATILI sa parehong presyo habang kumakain ng
      prints (refill/absorption): TUNAY na supply — ang umiiral na
      hidden/big-seller semantics ang tama.
  WALL_EATEN — ang mga print sa/lampas sa wall price ay >= malaking bahagi ng
      displayed size, nawala ang wall at WALANG kapalit sa band sa itaas: ang
      pader ay tinuluyan ng tunay na demand — ito ang punch signal ni Ross;
      HINDI na dapat mag-veto ang big-seller read sa window na ito.
  WALL_NONE — walang kwalipikadong wall / kulang ang datos (fail-open).

PURE ang classifier (walang I/O); may hiwalay na bounded DB reader. Adaptive
ang mga threshold (batch median / ATR-scaled) — ang mga floor lang ang
dokumentadong base. Fail-open sa lahat ng error: kulang na datos ay hindi
kailanman pumipigil ng entry o nag-iimbento ng estado.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

logger = logging.getLogger(__name__)

WALL_NONE = "WALL_NONE"
SPOOF_WALL_ACTIVE = "SPOOF_WALL_ACTIVE"
ICEBERG_REAL = "ICEBERG_REAL"
WALL_EATEN = "WALL_EATEN"

# Dokumentadong mga base (floors — ang mga aktwal na cut ay adaptive):
_WALL_SIZE_MULT_FLOOR = 3.0     # wall >= 3x ng median ladder level size
_WALL_BAND_PCT_FLOOR = 0.05     # wall sa loob ng 5% (o 2xATR%) sa itaas ng best ask
                                # (ang MEMX na "10-20c sa itaas" sa $3-10 movers
                                # ay 2-6% — ang 2% ay nakaliligtaan ang repeg hop)
_REPEG_MIN_EVENTS = 2           # >=2 pataas na repeg = aktibong spoof
_EATEN_PRINT_FRAC = 0.8         # prints >= 80% ng displayed size = kinain
_CANCEL_PRINT_FRAC = 0.25       # prints < 25% ng size nang mawala = kanselado
_ICEBERG_MIN_SNAPSHOTS = 3      # wall na nanatili sa >=3 sunod na snapshot


@dataclass
class _WallTrack:
    price: float
    size: float
    seen: int = 1
    absorbed: float = 0.0
    events: list[str] = field(default_factory=list)


def _find_wall(
    asks: list[list[float]] | None,
    *,
    best_ask: float | None,
    band_pct: float,
    size_floor: float,
) -> tuple[float, float] | None:
    """Ang pinakamalaking kwalipikadong ask level sa band sa itaas ng best ask."""
    if not asks or best_ask is None or best_ask <= 0:
        return None
    cand: tuple[float, float] | None = None
    for lvl in asks:
        try:
            px, sz = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px < best_ask or px > best_ask * (1.0 + band_pct):
            continue
        if sz < size_floor:
            continue
        if cand is None or sz > cand[1]:
            cand = (px, sz)
    return cand


def classify_repeg_wall(
    snapshots: list[dict[str, Any]],
    prints: list[tuple[datetime, float, float]],
    *,
    atr_pct: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Uriin ang wall behavior sa isang window ng snapshots + prints.

    ``snapshots``: time-ordered, bawat isa {"observed_at": dt, "ask_top": float,
    "asks": [[px, sz], ...]}. ``prints``: (ts, price, size), time-ordered.
    Pure; fail-open sa WALL_NONE."""
    debug: dict[str, Any] = {"snapshots": len(snapshots), "prints": len(prints)}
    try:
        if len(snapshots) < 2:
            return WALL_NONE, debug
        band_pct = max(_WALL_BAND_PCT_FLOOR, 2.0 * float(atr_pct or 0.0))
        all_sizes = [
            float(lvl[1])
            for snap in snapshots
            for lvl in (snap.get("asks") or [])
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2
        ]
        if not all_sizes:
            return WALL_NONE, debug
        size_floor = _WALL_SIZE_MULT_FLOOR * float(median(all_sizes))
        debug["size_floor"] = round(size_floor, 1)

        repeg_events = 0
        eaten = False
        track: _WallTrack | None = None
        for i in range(len(snapshots)):
            snap = snapshots[i]
            wall = _find_wall(
                snap.get("asks"),
                best_ask=snap.get("ask_top"),
                band_pct=band_pct,
                size_floor=size_floor,
            )
            if track is None:
                if wall is not None:
                    track = _WallTrack(price=wall[0], size=wall[1])
                continue
            t_prev = snapshots[i - 1].get("observed_at")
            t_cur = snap.get("observed_at")
            printed_at_wall = sum(
                sz for (ts, px, sz) in prints
                if t_prev is not None and t_cur is not None
                and t_prev < ts <= t_cur and px >= track.price * 0.999
            )
            if wall is not None and abs(wall[0] - track.price) <= track.price * 1e-4:
                # nanatili sa parehong presyo — absorption/iceberg candidate
                track.seen += 1
                track.absorbed += printed_at_wall
                track.size = max(track.size, wall[1])
                continue
            # nawala (o lumipat) ang wall mula sa dating presyo
            if printed_at_wall >= _EATEN_PRINT_FRAC * track.size and (
                wall is None or wall[0] <= track.price
            ):
                track.events.append("eaten")
                eaten = True
                track = _WallTrack(price=wall[0], size=wall[1]) if wall else None
                continue
            if (
                wall is not None
                and wall[0] > track.price
                and wall[1] >= 0.5 * track.size
                and printed_at_wall < _CANCEL_PRINT_FRAC * track.size
            ):
                # kinansela at inakyat nang halos walang prints = REPEG
                repeg_events += 1
                track.events.append("repeg_up")
                track = _WallTrack(price=wall[0], size=wall[1])
                continue
            track = _WallTrack(price=wall[0], size=wall[1]) if wall else track

        debug["repeg_events"] = repeg_events
        debug["eaten"] = eaten
        if track is not None:
            debug["wall_price"] = track.price
            debug["wall_seen"] = track.seen
            debug["wall_absorbed"] = round(track.absorbed, 1)

        if repeg_events >= _REPEG_MIN_EVENTS:
            return SPOOF_WALL_ACTIVE, debug
        if eaten:
            return WALL_EATEN, debug
        if (
            track is not None
            and track.seen >= _ICEBERG_MIN_SNAPSHOTS
            and track.absorbed > 0
        ):
            return ICEBERG_REAL, debug
        return WALL_NONE, debug
    except Exception:
        logger.debug("[repeg_wall] classify failed (fail-open)", exc_info=True)
        return WALL_NONE, debug


def read_repeg_wall_state(
    db: Any,
    symbol: str,
    *,
    as_of: datetime,
    window_seconds: float = 120.0,
    atr_pct: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Bounded DB read (<=3 min — ang iqfeed recency rule) + classify. Fail-open."""
    try:
        from sqlalchemy import text

        win = min(float(window_seconds), 180.0)
        lo = as_of - timedelta(seconds=win)
        sym = str(symbol or "").strip().upper()
        if not sym or db is None:
            return WALL_NONE, {"reason": "no_input"}
        snaps = db.execute(text(
            "SELECT observed_at, ask_top, asks_json FROM iqfeed_depth_snapshots "
            "WHERE symbol = :s AND observed_at > :lo AND observed_at <= :hi "
            "AND asks_json IS NOT NULL ORDER BY observed_at ASC LIMIT 400"
        ), {"s": sym, "lo": lo, "hi": as_of}).fetchall()
        if len(snaps) < 2:
            return WALL_NONE, {"reason": "insufficient_snapshots", "n": len(snaps)}
        ticks = db.execute(text(
            "SELECT observed_at, price, size FROM iqfeed_trade_ticks "
            "WHERE symbol = :s AND observed_at > :lo AND observed_at <= :hi "
            "ORDER BY observed_at ASC LIMIT 20000"
        ), {"s": sym, "lo": lo, "hi": as_of}).fetchall()
        snapshots = [
            {"observed_at": r[0], "ask_top": r[1], "asks": r[2]}
            for r in snaps
        ]
        prints = [
            (r[0], float(r[1] or 0.0), float(r[2] or 0.0)) for r in ticks
        ]
        return classify_repeg_wall(snapshots, prints, atr_pct=atr_pct)
    except Exception:
        logger.debug("[repeg_wall] read failed (fail-open)", exc_info=True)
        return WALL_NONE, {"reason": "read_error"}
