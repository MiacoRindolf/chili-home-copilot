"""Pydantic schemas for API and planner. Re-export for backward compatibility."""
from ..modules import is_module_enabled

from .household import (
    ChoreCreate,
    ChoreOut,
    BirthdayCreate,
    BirthdayOut,
    AddChoreBody,
    UpdateChoreBody,
    AddBirthdayBody,
    PairRequestBody,
    PairVerifyBody,
)

__all__ = [
    "ChoreCreate",
    "ChoreOut",
    "BirthdayCreate",
    "BirthdayOut",
    "AddChoreBody",
    "UpdateChoreBody",
    "AddBirthdayBody",
    "PairRequestBody",
    "PairVerifyBody",
]

# The action planner (llm_planner.py) always needs validate_plan available,
# even if the planner UI module is disabled, so we import planner schemas
# unconditionally but only re-export them when the planner module is enabled.
from .planner import (  # type: ignore[assignment]
    validate_plan,
    ProjectBody,
    MemberBody,
    RoleBody,
    TaskBody,
    CommentBody,
    LabelBody,
    EmptyData,
    AddChoreData,
    MarkChoreDoneData,
    AddBirthdayData,
    AddPlanProjectData,
    AddPlanProjectWithTasksData,
    AddPlanTaskData,
    TaskSpec,
)

if is_module_enabled("planner"):
    __all__ += [
        "validate_plan",
        "ProjectBody",
        "MemberBody",
        "RoleBody",
        "TaskBody",
        "CommentBody",
        "LabelBody",
        "EmptyData",
        "AddChoreData",
        "MarkChoreDoneData",
        "AddBirthdayData",
        "AddPlanProjectData",
        "AddPlanProjectWithTasksData",
        "AddPlanTaskData",
        "TaskSpec",
    ]
