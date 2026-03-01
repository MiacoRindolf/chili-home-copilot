from datetime import date
from pydantic import BaseModel

class ChoreCreate(BaseModel):
    title: str

class ChoreOut(BaseModel):
    id: int
    title: str
    done: bool

    class Config:
        from_attributes = True

class BirthdayCreate(BaseModel):
    name: str
    date: date

class BirthdayOut(BaseModel):
    id: int
    name: str
    date: date

    class Config:
        from_attributes = True