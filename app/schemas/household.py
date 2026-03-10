"""Household API and form schemas: chores, birthdays, pairing."""
from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ChoreCreate(BaseModel):
    title: str


class ChoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    done: bool


class BirthdayCreate(BaseModel):
    name: str
    date: date


class BirthdayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    date: date


# Form / API request bodies (pages, pairing)
class AddChoreBody(BaseModel):
    title: str
    priority: str = "medium"
    due_date: Optional[str] = None
    recurrence: str = "none"
    assigned_to: Optional[int] = None


class UpdateChoreBody(BaseModel):
    title: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
    recurrence: Optional[str] = None
    assigned_to: Optional[int] = None


class AddBirthdayBody(BaseModel):
    name: str
    date: str


class PairRequestBody(BaseModel):
    email: str


class PairVerifyBody(BaseModel):
    code: str
    label: str = "Unknown Device"
