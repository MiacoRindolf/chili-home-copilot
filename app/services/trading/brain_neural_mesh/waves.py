"""Group activation queue events into propagation waves (pure helpers)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional


def _parse_created_at(ev: dict[str, Any]) -> Optional[datetime]:
    raw = ev.get("created_at")
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def group_activation_events_into_waves(
    events: list[dict[str, Any]],
    *,
    time_window_sec: float = 2.0,
) -> list[dict[str, Any]]:
    """Cluster activation events into waves.

    Rules:
    - Rows sharing the same non-empty ``correlation_id`` belong to one wave.
    - Rows with null/empty correlation_id chain into the same wave when each
      event is within ``time_window_sec`` of the previous event in that chain
      (chronological order).

    ``events`` should be recent-first or arbitrary order; they are sorted
    internally by ``created_at`` ascending (missing times last).

    Returns newest wave first. Each wave dict:
    ``wave_id``, ``correlation_id`` (optional), ``source_node_ids``,
    ``event_count``, ``started_at``, ``ended_at`` (ISO or None).
    """
    if not events:
        return []

    decorated: list[tuple[Optional[datetime], int, dict[str, Any]]] = []
    for i, ev in enumerate(events):
        decorated.append((_parse_created_at(ev), i, ev))
    decorated.sort(key=lambda t: (t[0] is None, t[0] or datetime.min, t[1]))

    # correlation_id -> wave index
    corr_wave: dict[str, int] = {}
    waves: list[dict[str, Any]] = []
    window = timedelta(seconds=max(0.1, float(time_window_sec)))

    for _ts, _i, ev in decorated:
        ts = _parse_created_at(ev)
        cid = ev.get("correlation_id")
        if isinstance(cid, str):
            cid = cid.strip() or None
        else:
            cid = None
        sid = ev.get("source_node_id")
        if isinstance(sid, str) and sid.strip():
            src = sid.strip()
        else:
            src = None

        widx: Optional[int] = None
        if cid:
            widx = corr_wave.get(cid)
        if widx is None and ts is not None and not cid:
            if waves:
                last = waves[-1]
                end_s = last.get("ended_at")
                end_dt = _parse_created_at({"created_at": end_s}) if end_s else None
                if end_dt is not None and ts - end_dt <= window:
                    widx = len(waves) - 1

        if widx is None:
            widx = len(waves)
            wid = f"w{widx}"
            if cid:
                corr_wave[cid] = widx
            waves.append(
                {
                    "wave_id": wid,
                    "correlation_id": cid,
                    "source_node_ids": [],
                    "event_count": 0,
                    "started_at": None,
                    "ended_at": None,
                }
            )

        w = waves[widx]
        w["event_count"] = int(w["event_count"]) + 1
        if src and src not in w["source_node_ids"]:
            w["source_node_ids"].append(src)
        if ts:
            iso = ts.isoformat()
            if w["started_at"] is None or iso < str(w["started_at"]):
                w["started_at"] = iso
            if w["ended_at"] is None or iso > str(w["ended_at"]):
                w["ended_at"] = iso

    # Newest-first: sort waves by ended_at desc
    def _end_key(w: dict[str, Any]) -> datetime:
        e = _parse_created_at({"created_at": w.get("ended_at")})
        return e or datetime.min

    waves.sort(key=_end_key, reverse=True)
    return waves


def derive_overlay_hot_pulse_from_waves(
    waves: list[dict[str, Any]],
    outbound_by_source: dict[str, list[str]],
) -> tuple[list[str], list[str], Optional[dict[str, Any]]]:
    """Hot node ids and edge pulse keys from the newest wave (pure).

    ``waves`` must be newest-first (as returned by ``group_activation_events_into_waves``).
    """
    last_wave = waves[0] if waves else None
    hot: list[str] = []
    pulse_keys: list[str] = []
    if last_wave:
        hot = list(last_wave.get("source_node_ids") or [])
        pulse_keys = edge_pulse_keys_for_sources(hot, outbound_by_source)
    return hot, pulse_keys, last_wave


def edge_pulse_keys_for_sources(
    source_ids: list[str],
    outbound_by_source: dict[str, list[str]],
) -> list[str]:
    """Build ``from->to`` keys for SVG edges matching current client convention."""
    keys: list[str] = []
    seen: set[str] = set()
    for src in source_ids:
        for tgt in outbound_by_source.get(src, []):
            k = f"{src}->{tgt}"
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys
