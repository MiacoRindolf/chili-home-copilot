"""Code Brain domain schemas: repos, insights, snapshots, hotspots, reviews."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class CodeRepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    path: str
    name: str
    language_stats: Optional[str] = None
    framework_tags: Optional[str] = None
    file_count: int = 0
    total_lines: int = 0
    last_indexed: Optional[datetime] = None
    last_commit_hash: Optional[str] = None
    created_at: datetime
    active: bool


class CodeInsightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: Optional[int] = None
    user_id: Optional[int] = None
    category: str
    description: str
    confidence: float
    evidence_count: int
    evidence_files: Optional[str] = None
    active: bool
    last_seen: datetime
    created_at: datetime


class CodeSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    file_path: str
    language: Optional[str] = None
    line_count: int = 0
    function_count: int = 0
    class_count: int = 0
    complexity_score: float = 0.0
    last_modified: Optional[datetime] = None
    snapshot_date: datetime


class CodeHotspotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    file_path: str
    churn_score: float = 0.0
    complexity_score: float = 0.0
    combined_score: float = 0.0
    commit_count: int = 0
    last_commit_date: Optional[datetime] = None
    snapshot_date: datetime


class CodeReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    user_id: Optional[int] = None
    commit_hash: str
    author: Optional[str] = None
    summary: Optional[str] = None
    findings_json: Optional[str] = None
    overall_score: float = 5.0
    reviewed_at: datetime


class CodeDepAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    package_name: str
    current_version: Optional[str] = None
    latest_version: Optional[str] = None
    severity: str = "info"
    alert_type: str = "outdated"
    ecosystem: str = "pip"
    resolved: bool = False
    detected_at: datetime


class CodeQualitySnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    total_files: int = 0
    total_lines: int = 0
    avg_complexity: float = 0.0
    max_complexity: float = 0.0
    test_file_count: int = 0
    test_ratio: float = 0.0
    hotspot_count: int = 0
    insight_count: int = 0
    recorded_at: datetime
