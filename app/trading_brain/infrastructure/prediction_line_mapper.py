"""Map legacy prediction dicts ↔ mirror DTOs (Phase 4)."""

from __future__ import annotations

import hashlib

from ..schemas.prediction_snapshot import PredictionLineWriteDTO


def prediction_universe_fingerprint(tickers: list[str]) -> str:
    """SHA-256 hex of sorted uppercased tickers (effective batch universe)."""
    norm = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
    body = ",".join(norm).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def legacy_prediction_rows_to_dtos(legacy_rows: list[dict]) -> list[PredictionLineWriteDTO]:
    """Convert post-sort legacy list[dict] to write DTOs (`sort_rank` = list index)."""
    out: list[PredictionLineWriteDTO] = []
    for rank, row in enumerate(legacy_rows):
        signals = row.get("signals") or []
        if not isinstance(signals, list):
            signals = []
        mp = row.get("matched_patterns") or []
        if not isinstance(mp, list):
            mp = []
        out.append(
            PredictionLineWriteDTO(
                sort_rank=rank,
                ticker=str(row.get("ticker") or ""),
                score=float(row.get("score", 0.0)),
                confidence=row.get("confidence"),
                direction=row.get("direction"),
                price=row.get("price"),
                meta_ml_probability=row.get("meta_ml_probability"),
                vix_regime=row.get("vix_regime"),
                signals=[str(s) for s in signals],
                matched_patterns=[dict(x) for x in mp if isinstance(x, dict)],
                suggested_stop=row.get("suggested_stop"),
                suggested_target=row.get("suggested_target"),
                risk_reward=row.get("risk_reward"),
                position_size_pct=row.get("position_size_pct"),
            )
        )
    return out
