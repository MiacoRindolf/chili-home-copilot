"""Apply validated evidence hypotheses to ScanPattern (dry-run first)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PatternEvidenceHypothesis, ScanPattern
from .pattern_evidence_service import log_pattern_evidence_event

logger = logging.getLogger(__name__)


def apply_evidence_hypothesis(
    db: Session,
    hypothesis_id: int,
    *,
    dry_run: bool = True,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Interpret predicate_json and return proposed ScanPattern diff. If dry_run=False, apply safest change."""
    hyp = db.query(PatternEvidenceHypothesis).filter(PatternEvidenceHypothesis.id == hypothesis_id).first()
    if not hyp:
        return {"ok": False, "error": "hypothesis not found"}
    if dry_run:
        if hyp.status not in ("validated", "proposed"):
            return {"ok": False, "error": f"status {hyp.status} not applicable for dry-run preview"}
    elif hyp.status != "validated":
        return {"ok": False, "error": "apply requires status=validated (run walk-forward first)"}

    pat = db.query(ScanPattern).filter(ScanPattern.id == hyp.scan_pattern_id).first()
    if not pat:
        return {"ok": False, "error": "pattern not found"}

    pred = hyp.predicate_json or {}
    out: dict[str, Any] = {"ok": True, "dry_run": dry_run, "scan_pattern_id": pat.id, "changes": []}

    if pred.get("type") == "median_split":
        feat = pred.get("feature_key")
        favor = pred.get("favor")
        # Safest v1 action: bump score_boost slightly when evidence favors conditioning
        old_boost = float(pat.score_boost or 0)
        delta = 0.25
        new_boost = min(3.0, old_boost + delta)
        out["changes"].append({
            "field": "score_boost",
            "from": old_boost,
            "to": new_boost,
            "reason": f"evidence median_split {feat} favor={favor}",
        })
        rules_hint = {
            "note": "Consider adding filter condition from analytics",
            "feature": feat,
            "favor": favor,
        }
        out["changes"].append({"field": "rules_json_hint", "value": rules_hint})

        if not dry_run:
            pat.score_boost = new_boost
            try:
                rules = json.loads(pat.rules_json or "{}")
            except json.JSONDecodeError:
                rules = {}
            meta = rules.setdefault("_analytics_meta", [])
            if isinstance(meta, list):
                meta.append(rules_hint)
            pat.rules_json = json.dumps(rules)
            db.commit()
            hyp.status = "applied"
            hyp.updated_at = datetime.utcnow()
            db.commit()
            log_pattern_evidence_event(
                db, user_id, "pattern_apply",
                f"Applied evidence hypothesis {hypothesis_id} to pattern {pat.id} score_boost {old_boost}->{new_boost}",
            )
    else:
        out["ok"] = False
        out["error"] = "unsupported predicate"

    return out
