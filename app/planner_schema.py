from typing import Literal, Optional, Union, Dict, Any
from pydantic import BaseModel, Field, ValidationError, ConfigDict

ActionType = Literal[
    "add_chore",
    "list_chores",
    "list_chores_pending",
    "mark_chore_done",
    "add_birthday",
    "list_birthdays",
    "unknown",
]

class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject extra keys

    type: ActionType
    data: Dict[str, Any] = Field(default_factory=dict)
    reply: str

def validate_plan(obj: dict) -> Optional[Plan]:
    try:
        return Plan.model_validate(obj)
    except ValidationError:
        return None