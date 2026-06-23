"""Train the momentum META-LABEL de-rate from the captured (entry-features, outcome) dataset.

Re-run this as the dataset grows (replay JSON + LIVE MomentumAutomationOutcome entry
features). It trains + validates behind the min-sample/AUC/permutation GATE in meta_label.py
and writes the model JSON; the live/replay sizing reads it and applies size_multiplier ONLY
when the model PASSED its gate (go=True). Until then the lane is byte-identical (multiplier 1.0).

Usage (in-container):  python scripts/train_meta_label.py
"""
from __future__ import annotations

import json
import os

from app.services.trading.momentum_neural.meta_label import save_model, train_meta_label

REPLAY = os.environ.get("DISC_DATASET", "/app/data/_disc_dataset.json")
OUT = os.environ.get("META_MODEL_OUT", "/app/data/_meta_label_model.json")


def _load_rows() -> list[dict]:
    rows: list[dict] = []
    if os.path.exists(REPLAY):
        try:
            rows.extend(json.load(open(REPLAY)))
        except Exception as e:
            print("replay dataset read skipped:", e)
    try:
        from app.db import SessionLocal
        from app.models.trading import MomentumAutomationOutcome as MAO

        db = SessionLocal()
        eq = ["robinhood_spot", "alpaca_spot", "robinhood_agentic_mcp"]
        q = db.query(MAO).filter(MAO.execution_family.in_(eq), MAO.return_bps.isnot(None))
        live = 0
        for o in q.limit(20000):
            ers = o.entry_regime_snapshot_json
            if isinstance(ers, dict) and isinstance(ers.get("features"), dict) and ers["features"]:
                rows.append({
                    "return_bps": float(o.return_bps),
                    "features": ers["features"],
                    "day": (str(o.terminal_at)[:10] if o.terminal_at else ""),
                    "sym": o.symbol,
                })
                live += 1
        db.close()
        print(f"live MAO rows with entry features: {live}")
    except Exception as e:
        print("live MAO read skipped:", e)
    return rows


def main() -> None:
    rows = _load_rows()
    print(f"dataset rows (replay + live): {len(rows)}")
    model = train_meta_label(rows)
    verdict = {k: v for k, v in model.items() if k not in ("coef", "mean", "std", "median")}
    print("VERDICT:", json.dumps(verdict, default=str, indent=2))
    save_model(model, OUT)
    print(f"saved {OUT} | go={model.get('go')} status={model.get('status')}")


if __name__ == "__main__":
    main()
