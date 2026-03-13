"""Mine conventions and architectural patterns from indexed data."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeInsight, CodeRepo, CodeSnapshot, CodeHotspot

logger = logging.getLogger(__name__)


def _upsert_insight(
    db: Session,
    repo_id: int,
    category: str,
    description: str,
    confidence: float,
    evidence_count: int = 1,
    evidence_files: Optional[List[str]] = None,
    user_id: Optional[int] = None,
) -> CodeInsight:
    """Create or update an insight (match on repo_id + category + description)."""
    existing = (
        db.query(CodeInsight)
        .filter(
            CodeInsight.repo_id == repo_id,
            CodeInsight.category == category,
            CodeInsight.description == description,
        )
        .first()
    )
    if existing:
        existing.confidence = confidence
        existing.evidence_count = evidence_count
        existing.evidence_files = json.dumps(evidence_files) if evidence_files else None
        existing.last_seen = datetime.utcnow()
        existing.active = True
        return existing

    ins = CodeInsight(
        repo_id=repo_id,
        user_id=user_id,
        category=category,
        description=description,
        confidence=confidence,
        evidence_count=evidence_count,
        evidence_files=json.dumps(evidence_files) if evidence_files else None,
    )
    db.add(ins)
    return ins


def mine_insights(db: Session, repo_id: int, user_id: Optional[int] = None) -> Dict:
    """Analyze indexed data to discover patterns and conventions."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    snapshots = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).all()
    if not snapshots:
        return {"discovered": 0}

    discovered = 0

    # --- Language distribution insight ---
    lang_counter: Counter = Counter()
    for s in snapshots:
        if s.language:
            lang_counter[s.language] += 1
    if lang_counter:
        primary = lang_counter.most_common(1)[0]
        pct = primary[1] / len(snapshots) * 100
        _upsert_insight(
            db, repo_id, "convention",
            f"Primary language: {primary[0]} ({pct:.0f}% of files)",
            confidence=min(pct / 100, 1.0),
            evidence_count=primary[1],
            user_id=user_id,
        )
        discovered += 1

    # --- Framework detection ---
    if repo.framework_tags:
        for fw in repo.framework_tags.split(","):
            _upsert_insight(
                db, repo_id, "architecture",
                f"Uses framework: {fw.strip()}",
                confidence=0.9,
                user_id=user_id,
            )
            discovered += 1

    # --- File organization patterns ---
    dir_patterns: Counter = Counter()
    for s in snapshots:
        parts = s.file_path.split("/")
        if len(parts) > 1:
            dir_patterns[parts[0]] += 1

    for dirname, count in dir_patterns.most_common(10):
        if count >= 3:
            _upsert_insight(
                db, repo_id, "architecture",
                f"Directory '{dirname}/' contains {count} files",
                confidence=0.7,
                evidence_count=count,
                user_id=user_id,
            )
            discovered += 1

    # --- Test structure ---
    test_files = [s for s in snapshots if "test" in s.file_path.lower()]
    if test_files:
        test_patterns = Counter()
        for tf in test_files:
            name = tf.file_path.split("/")[-1]
            if name.startswith("test_"):
                test_patterns["test_<module>"] += 1
            elif name.endswith("_test.py") or name.endswith(".test.js") or name.endswith(".test.ts"):
                test_patterns["<module>.test"] += 1
            elif name.endswith("_spec.rb") or name.endswith(".spec.ts") or name.endswith(".spec.js"):
                test_patterns["<module>.spec"] += 1
        if test_patterns:
            dominant = test_patterns.most_common(1)[0]
            _upsert_insight(
                db, repo_id, "convention",
                f"Test naming: {dominant[0]} ({dominant[1]} files)",
                confidence=min(dominant[1] / max(len(test_files), 1), 1.0),
                evidence_count=dominant[1],
                evidence_files=[t.file_path for t in test_files[:20]],
                user_id=user_id,
            )
            discovered += 1

    # --- Init exports pattern (Python) ---
    init_files = [s for s in snapshots if s.file_path.endswith("__init__.py")]
    if len(init_files) >= 2:
        _upsert_insight(
            db, repo_id, "convention",
            f"Uses Python package __init__.py exports ({len(init_files)} packages)",
            confidence=0.8,
            evidence_count=len(init_files),
            evidence_files=[i.file_path for i in init_files[:20]],
            user_id=user_id,
        )
        discovered += 1

    # --- Complexity insights ---
    high_complexity = [s for s in snapshots if s.complexity_score > 50]
    if high_complexity:
        _upsert_insight(
            db, repo_id, "quality",
            f"{len(high_complexity)} files have high complexity (score > 50)",
            confidence=0.85,
            evidence_count=len(high_complexity),
            evidence_files=[h.file_path for h in sorted(high_complexity, key=lambda x: -x.complexity_score)[:20]],
            user_id=user_id,
        )
        discovered += 1

    # --- Hotspot-based insights ---
    hotspots = (
        db.query(CodeHotspot)
        .filter(CodeHotspot.repo_id == repo_id)
        .order_by(CodeHotspot.combined_score.desc())
        .limit(5)
        .all()
    )
    if hotspots:
        files = [h.file_path for h in hotspots]
        _upsert_insight(
            db, repo_id, "quality",
            f"Top hotspots (high churn + complexity): {', '.join(f.split('/')[-1] for f in files[:3])}",
            confidence=0.8,
            evidence_count=len(hotspots),
            evidence_files=files,
            user_id=user_id,
        )
        discovered += 1

    # --- Average file size ---
    avg_lines = sum(s.line_count for s in snapshots) / max(len(snapshots), 1)
    if avg_lines > 200:
        _upsert_insight(
            db, repo_id, "quality",
            f"Average file length is {avg_lines:.0f} lines (consider splitting large files)",
            confidence=0.7,
            evidence_count=len(snapshots),
            user_id=user_id,
        )
        discovered += 1

    db.commit()
    return {"discovered": discovered}


def get_insights(
    db: Session,
    repo_id: Optional[int] = None,
    category: Optional[str] = None,
    active_only: bool = True,
) -> List[Dict]:
    """Retrieve stored insights with optional filtering."""
    q = db.query(CodeInsight)
    if active_only:
        q = q.filter(CodeInsight.active.is_(True))
    if repo_id is not None:
        q = q.filter(CodeInsight.repo_id == repo_id)
    if category:
        q = q.filter(CodeInsight.category == category)

    results = q.order_by(CodeInsight.confidence.desc()).limit(100).all()
    return [
        {
            "id": i.id,
            "repo_id": i.repo_id,
            "category": i.category,
            "description": i.description,
            "confidence": i.confidence,
            "evidence_count": i.evidence_count,
            "evidence_files": json.loads(i.evidence_files) if i.evidence_files else [],
            "last_seen": i.last_seen.isoformat() if i.last_seen else None,
        }
        for i in results
    ]
