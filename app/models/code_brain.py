"""Code Brain models: repos, insights, snapshots, hotspots."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from ..db import Base


class CodeRepo(Base):
    """A registered local repository the Code Brain indexes."""
    __tablename__ = "code_repos"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    path: str = Column(String(500), nullable=False, unique=True)
    name: str = Column(String(200), nullable=False)
    language_stats: Optional[str] = Column(Text, nullable=True)       # JSON: {"python": 42, "javascript": 18, ...}
    framework_tags: Optional[str] = Column(String(500), nullable=True)  # comma-separated: "fastapi,sqlalchemy,jinja2"
    file_count: int = Column(Integer, default=0)
    total_lines: int = Column(Integer, default=0)
    last_indexed: Optional[datetime] = Column(DateTime, nullable=True)
    last_commit_hash: Optional[str] = Column(String(50), nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    active: bool = Column(Boolean, default=True, nullable=False)


class CodeInsight(Base):
    """A discovered pattern or convention from a codebase."""
    __tablename__ = "code_insights"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: Optional[int] = Column(Integer, nullable=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    category: str = Column(String(50), nullable=False)   # "convention", "architecture", "quality", "dependency", "pattern"
    description: str = Column(Text, nullable=False)
    confidence: float = Column(Float, nullable=False, default=0.5)
    evidence_count: int = Column(Integer, nullable=False, default=1)
    evidence_files: Optional[str] = Column(Text, nullable=True)  # JSON list of file paths
    active: bool = Column(Boolean, default=True, nullable=False)
    last_seen: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeSnapshot(Base):
    """Per-file metrics captured during indexing."""
    __tablename__ = "code_snapshots"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    file_path: str = Column(String(500), nullable=False)
    language: Optional[str] = Column(String(30), nullable=True)
    line_count: int = Column(Integer, default=0)
    function_count: int = Column(Integer, default=0)
    class_count: int = Column(Integer, default=0)
    complexity_score: float = Column(Float, default=0.0)
    last_modified: Optional[datetime] = Column(DateTime, nullable=True)
    snapshot_date: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeHotspot(Base):
    """High-churn or high-complexity files flagged for attention."""
    __tablename__ = "code_hotspots"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    file_path: str = Column(String(500), nullable=False)
    churn_score: float = Column(Float, default=0.0)      # normalized commit frequency
    complexity_score: float = Column(Float, default=0.0)
    combined_score: float = Column(Float, default=0.0)    # churn * complexity
    commit_count: int = Column(Integer, default=0)
    last_commit_date: Optional[datetime] = Column(DateTime, nullable=True)
    snapshot_date: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeLearningEvent(Base):
    """Timeline entry for code brain learning activity."""
    __tablename__ = "code_learning_events"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    repo_id: Optional[int] = Column(Integer, nullable=True)
    event_type: str = Column(String(30), nullable=False)  # "index", "insight", "hotspot", "error"
    description: str = Column(Text, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeDependency(Base):
    """Directed edge in the architecture import graph."""
    __tablename__ = "code_dependencies"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    source_file: str = Column(String(500), nullable=False)
    target_file: str = Column(String(500), nullable=False)
    import_name: Optional[str] = Column(String(300), nullable=True)
    is_circular: bool = Column(Boolean, default=False, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeQualitySnapshot(Base):
    """Point-in-time aggregate quality metrics for trend tracking."""
    __tablename__ = "code_quality_snapshots"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    total_files: int = Column(Integer, default=0)
    total_lines: int = Column(Integer, default=0)
    avg_complexity: float = Column(Float, default=0.0)
    max_complexity: float = Column(Float, default=0.0)
    test_file_count: int = Column(Integer, default=0)
    test_ratio: float = Column(Float, default=0.0)
    hotspot_count: int = Column(Integer, default=0)
    insight_count: int = Column(Integer, default=0)
    recorded_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeReview(Base):
    """LLM-generated code review for a git commit."""
    __tablename__ = "code_reviews"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    commit_hash: str = Column(String(50), nullable=False, index=True)
    author: Optional[str] = Column(String(200), nullable=True)
    summary: Optional[str] = Column(Text, nullable=True)
    findings_json: Optional[str] = Column(Text, nullable=True)
    overall_score: float = Column(Float, default=5.0)
    reviewed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeDepAlert(Base):
    """Dependency health alert (outdated, vulnerable, etc.)."""
    __tablename__ = "code_dep_alerts"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    package_name: str = Column(String(200), nullable=False)
    current_version: Optional[str] = Column(String(50), nullable=True)
    latest_version: Optional[str] = Column(String(50), nullable=True)
    severity: str = Column(String(20), nullable=False, default="info")
    alert_type: str = Column(String(30), nullable=False, default="outdated")
    ecosystem: str = Column(String(10), nullable=False, default="pip")
    resolved: bool = Column(Boolean, default=False, nullable=False)
    detected_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CodeSearchEntry(Base):
    """Indexed function/class symbol for code search."""
    __tablename__ = "code_search_index"

    id: int = Column(Integer, primary_key=True, index=True)
    repo_id: int = Column(Integer, nullable=False, index=True)
    file_path: str = Column(String(500), nullable=False)
    symbol_name: str = Column(String(300), nullable=False, index=True)
    symbol_type: str = Column(String(20), nullable=False)
    signature: Optional[str] = Column(Text, nullable=True)
    docstring: Optional[str] = Column(Text, nullable=True)
    line_number: int = Column(Integer, default=0)
    indexed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
