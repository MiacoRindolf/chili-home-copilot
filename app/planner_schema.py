from typing import Literal, Union, Optional
from pydantic import BaseModel, Field, ValidationError, ConfigDict
from datetime import date

# --- Data payload schemas ---

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
    # accept "YYYY-MM-DD" as string and validate by parsing to date
    date: date

class AnswerFromDocsData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1)

class PairDeviceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

class IntercomBroadcastData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)

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
    UnknownPlan,
]


def validate_plan(obj: dict) -> Optional[dict]:
    """
    Returns a normalized dict with strict types if valid, else None.
    Note: Birthday 'date' becomes ISO string when dumped.
    """
    try:
        plan = BaseModel.model_validate(Plan, obj)  # Pydantic v2 union validation
    except Exception:
        # Fallback for some environments: validate by attempting each model
        try:
            for cls in (AddChorePlan, ListChoresPlan, ListChoresPendingPlan, MarkChoreDonePlan,
                        AddBirthdayPlan, ListBirthdaysPlan, AnswerFromDocsPlan, PairDevicePlan,
                        IntercomBroadcastPlan, UnknownPlan):
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
    # Normalize date (datetime.date) to "YYYY-MM-DD"
    if dumped["type"] == "add_birthday":
        dumped["data"]["date"] = dumped["data"]["date"].isoformat()
    return dumped