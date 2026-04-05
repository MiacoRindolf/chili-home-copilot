"""ML model registry for the trading brain.

Tracks model versions, supports shadow scoring (running old and new
models in parallel), and auto-rollback when a new model underperforms.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "ml_models"
_REGISTRY_FILE = _DATA_DIR / "registry.json"


@dataclass
class ModelVersion:
    """Metadata for a trained model version."""
    version_id: str
    model_type: str  # e.g. "pattern_meta_learner", "signal_scorer"
    trained_at: str
    metrics: dict[str, float] = field(default_factory=dict)
    is_active: bool = False
    is_shadow: bool = False
    file_path: str | None = None
    parent_version: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "model_type": self.model_type,
            "trained_at": self.trained_at,
            "metrics": self.metrics,
            "is_active": self.is_active,
            "is_shadow": self.is_shadow,
            "file_path": self.file_path,
            "parent_version": self.parent_version,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelVersion":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class ModelRegistry:
    """In-memory + file-backed model version registry."""

    def __init__(self):
        self._versions: dict[str, ModelVersion] = {}
        self._load()

    def _load(self) -> None:
        if _REGISTRY_FILE.exists():
            try:
                data = json.loads(_REGISTRY_FILE.read_text())
                for d in data.get("versions", []):
                    mv = ModelVersion.from_dict(d)
                    self._versions[mv.version_id] = mv
            except Exception as e:
                logger.warning("[model_registry] Failed to load: %s", e)

    def _save(self) -> None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {"versions": [v.to_dict() for v in self._versions.values()]}
        _REGISTRY_FILE.write_text(json.dumps(data, indent=2))

    def register(
        self,
        model_type: str,
        metrics: dict[str, float],
        *,
        file_path: str | None = None,
        parent_version: str | None = None,
        notes: str = "",
    ) -> ModelVersion:
        """Register a new model version."""
        vid = f"{model_type}_v{int(time.time())}"
        mv = ModelVersion(
            version_id=vid,
            model_type=model_type,
            trained_at=datetime.utcnow().isoformat() + "Z",
            metrics=metrics,
            is_active=False,
            is_shadow=True,
            file_path=file_path,
            parent_version=parent_version,
            notes=notes,
        )
        self._versions[vid] = mv
        self._save()
        logger.info("[model_registry] Registered %s: %s", vid, metrics)
        return mv

    def promote(self, version_id: str) -> bool:
        """Promote a shadow model to active, demoting the current active."""
        mv = self._versions.get(version_id)
        if not mv:
            return False

        for v in self._versions.values():
            if v.model_type == mv.model_type and v.is_active:
                v.is_active = False
                v.is_shadow = False

        mv.is_active = True
        mv.is_shadow = False
        self._save()
        logger.info("[model_registry] Promoted %s to active", version_id)
        return True

    def rollback(self, model_type: str) -> str | None:
        """Roll back to the previous active version of a model type."""
        versions = sorted(
            [v for v in self._versions.values() if v.model_type == model_type],
            key=lambda v: v.trained_at,
            reverse=True,
        )
        current_active = next((v for v in versions if v.is_active), None)
        if current_active:
            current_active.is_active = False

        previous = next(
            (v for v in versions if v != current_active and not v.is_shadow),
            None,
        )
        if previous:
            previous.is_active = True
            self._save()
            logger.info("[model_registry] Rolled back %s to %s", model_type, previous.version_id)
            return previous.version_id
        return None

    def get_active(self, model_type: str) -> ModelVersion | None:
        """Get the current active version for a model type."""
        for v in self._versions.values():
            if v.model_type == model_type and v.is_active:
                return v
        return None

    def get_shadow(self, model_type: str) -> ModelVersion | None:
        """Get the current shadow version for a model type."""
        shadows = [v for v in self._versions.values() if v.model_type == model_type and v.is_shadow]
        return max(shadows, key=lambda v: v.trained_at) if shadows else None

    def check_shadow_vs_active(
        self,
        model_type: str,
        *,
        min_improvement_pct: float = 5.0,
        metric_key: str = "oos_accuracy",
    ) -> dict[str, Any]:
        """Compare shadow model against active. Auto-promote if better."""
        active = self.get_active(model_type)
        shadow = self.get_shadow(model_type)

        if not shadow:
            return {"action": "none", "reason": "no_shadow"}
        if not active:
            self.promote(shadow.version_id)
            return {"action": "promoted", "reason": "no_active", "version": shadow.version_id}

        active_metric = active.metrics.get(metric_key, 0)
        shadow_metric = shadow.metrics.get(metric_key, 0)

        if active_metric <= 0:
            self.promote(shadow.version_id)
            return {"action": "promoted", "reason": "active_has_no_metric", "version": shadow.version_id}

        improvement = (shadow_metric - active_metric) / active_metric * 100

        if improvement >= min_improvement_pct:
            self.promote(shadow.version_id)
            return {
                "action": "promoted",
                "reason": f"shadow {shadow_metric:.2f} > active {active_metric:.2f} (+{improvement:.1f}%)",
                "version": shadow.version_id,
            }

        return {
            "action": "keep_active",
            "active_metric": active_metric,
            "shadow_metric": shadow_metric,
            "improvement_pct": round(improvement, 2),
        }

    def list_versions(self, model_type: str | None = None) -> list[dict[str, Any]]:
        """List all versions, optionally filtered by type."""
        versions = self._versions.values()
        if model_type:
            versions = [v for v in versions if v.model_type == model_type]
        return [v.to_dict() for v in sorted(versions, key=lambda v: v.trained_at, reverse=True)]

    def get_summary(self) -> dict[str, Any]:
        """Summary of the registry state."""
        types: dict[str, dict[str, Any]] = {}
        for v in self._versions.values():
            if v.model_type not in types:
                types[v.model_type] = {"total": 0, "active": None, "shadow": None}
            types[v.model_type]["total"] += 1
            if v.is_active:
                types[v.model_type]["active"] = v.version_id
            if v.is_shadow:
                types[v.model_type]["shadow"] = v.version_id
        return {"model_types": types, "total_versions": len(self._versions)}


# Singleton registry instance
_registry: ModelRegistry | None = None


def get_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry
