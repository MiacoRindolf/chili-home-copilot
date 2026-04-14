"""Coding Task domain schemas: coding profiles, briefs, suggestions, validation."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PlanTaskCodingProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: int
    repo_index: int = 0
    code_repo_id: Optional[int] = None
    sub_path: str = ""
    brief_approved_at: Optional[datetime] = None
    updated_at: datetime


class TaskClarificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    question: str
    answer: Optional[str] = None
    status: str = "open"
    sort_order: int = 0
    created_at: datetime
    updated_at: datetime


class CodingTaskBriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    body: str = ""
    version: int = 1
    created_by: Optional[int] = None
    created_at: datetime


class CodingTaskValidationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    trigger_source: str = "manual"
    status: str = "pending"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    timed_out: bool = False
    error_message: Optional[str] = None


class CodingAgentSuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    user_id: int
    created_at: datetime
    model: str = ""
    response_text: str = ""
    diffs_json: str = "[]"
    files_changed_json: str = "[]"


class CodingAgentSuggestionApplyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    suggestion_id: int
    task_id: int
    user_id: int
    created_at: datetime
    dry_run: bool = False
    status: str
    message: str = ""


class CodingBlockerReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    run_id: Optional[int] = None
    category: str = "validation"
    severity: str = "error"
    summary: str
