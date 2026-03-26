"""Planner action validation (LLM plan schema) and planner API request bodies."""
from datetime import date
from typing import Literal, Union, Optional, Any

from pydantic import BaseModel, Field, ValidationError, ConfigDict, field_validator


# --- Data payload schemas (LLM planner) ---

class EmptyData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddChoreData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)


class MarkChoreDoneData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)


class AddBirthdayData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    date: date


class AnswerFromDocsData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1)


class PairDeviceData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntercomBroadcastData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)


class WebSearchData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)


class AddPlanProjectData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)


class TaskSpec(BaseModel):
    """One task when creating a project with tasks; description can include complexity, duration, reasoning."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    description: str = ""


class AddPlanProjectWithTasksData(BaseModel):
    """Create a project and add multiple tasks in one action."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    description: str = ""
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=30)

    @field_validator("tasks", mode="before")
    @classmethod
    def coerce_tasks(cls, v: Any) -> list[dict]:
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append({"title": item.strip(), "description": ""})
            elif isinstance(item, dict) and (item.get("title") or item.get("description")):
                out.append({
                    "title": (str(item.get("title", "")).strip() or "Task"),
                    "description": str(item.get("description", "")).strip(),
                })
        return out[:30]


class AddPlanTaskData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str = Field(min_length=1)
    title: str = Field(min_length=1)


class ListPlanProjectsData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnknownData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)


# --- Plan schemas (discriminated union by `type`) ---

class BasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=200)


class AddChorePlan(BasePlan):
    type: Literal["add_chore"]
    data: AddChoreData


class ListChoresPlan(BasePlan):
    type: Literal["list_chores"]
    data: EmptyData = Field(default_factory=EmptyData)


class ListChoresPendingPlan(BasePlan):
    type: Literal["list_chores_pending"]
    data: EmptyData = Field(default_factory=EmptyData)


class MarkChoreDonePlan(BasePlan):
    type: Literal["mark_chore_done"]
    data: MarkChoreDoneData


class AddBirthdayPlan(BasePlan):
    type: Literal["add_birthday"]
    data: AddBirthdayData


class ListBirthdaysPlan(BasePlan):
    type: Literal["list_birthdays"]
    data: EmptyData = Field(default_factory=EmptyData)


class AnswerFromDocsPlan(BasePlan):
    type: Literal["answer_from_docs"]
    data: AnswerFromDocsData


class PairDevicePlan(BasePlan):
    type: Literal["pair_device"]
    data: PairDeviceData = Field(default_factory=PairDeviceData)


class IntercomBroadcastPlan(BasePlan):
    type: Literal["intercom_broadcast"]
    data: IntercomBroadcastData


class WebSearchPlan(BasePlan):
    type: Literal["web_search"]
    data: WebSearchData


class AddPlanProjectPlan(BasePlan):
    type: Literal["add_plan_project"]
    data: AddPlanProjectData


class AddPlanProjectWithTasksPlan(BasePlan):
    type: Literal["add_plan_project_with_tasks"]
    data: AddPlanProjectWithTasksData


class AddPlanTaskPlan(BasePlan):
    type: Literal["add_plan_task"]
    data: AddPlanTaskData


class ListPlanProjectsPlan(BasePlan):
    type: Literal["list_plan_projects"]
    data: ListPlanProjectsData = Field(default_factory=ListPlanProjectsData)


class UnknownPlan(BasePlan):
    type: Literal["unknown"]
    data: UnknownData


Plan = Union[
    AddChorePlan,
    ListChoresPlan,
    ListChoresPendingPlan,
    MarkChoreDonePlan,
    AddBirthdayPlan,
    ListBirthdaysPlan,
    AnswerFromDocsPlan,
    PairDevicePlan,
    IntercomBroadcastPlan,
    WebSearchPlan,
    AddPlanProjectPlan,
    AddPlanProjectWithTasksPlan,
    AddPlanTaskPlan,
    ListPlanProjectsPlan,
    UnknownPlan,
]


def validate_plan(obj: dict) -> Optional[dict]:
    """
    Returns a normalized dict with strict types if valid, else None.
    Note: Birthday 'date' becomes ISO string when dumped.
    """
    try:
        plan = BaseModel.model_validate(Plan, obj)
    except Exception:
        try:
            for cls in (
                AddChorePlan, ListChoresPlan, ListChoresPendingPlan, MarkChoreDonePlan,
                AddBirthdayPlan, ListBirthdaysPlan, AnswerFromDocsPlan, PairDevicePlan,
                IntercomBroadcastPlan, WebSearchPlan,
                AddPlanProjectPlan, AddPlanProjectWithTasksPlan, AddPlanTaskPlan, ListPlanProjectsPlan,
                UnknownPlan,
            ):
                try:
                    plan = cls.model_validate(obj)
                    break
                except ValidationError:
                    continue
            else:
                return None
        except Exception:
            return None

    dumped = plan.model_dump()
    if dumped["type"] == "add_birthday":
        dumped["data"]["date"] = dumped["data"]["date"].isoformat()
    return dumped


# --- Planner API request bodies (routers/planner) ---

class ProjectBody(BaseModel):
    name: str
    description: Optional[str] = ""
    color: Optional[str] = "#6366f1"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = None
    key: Optional[str] = None


class MemberBody(BaseModel):
    user_id: int
    role: Optional[str] = "editor"


class RoleBody(BaseModel):
    role: str


class TaskBody(BaseModel):
    title: str
    description: Optional[str] = ""
    priority: Optional[str] = "medium"
    status: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    assigned_to: Optional[int] = None
    depends_on: Optional[int] = None
    progress: Optional[int] = None
    sort_order: Optional[int] = None
    parent_id: Optional[int] = None
    coding_workflow_mode: Optional[str] = None


class CommentBody(BaseModel):
    content: str


class LabelBody(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"
