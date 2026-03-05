"""Household: chores, birthdays, activity log, housemate profile, user memory, user status."""
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime

from ..db import Base


class Chore(Base):
    __tablename__ = "chores"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    done = Column(Boolean, default=False)
    priority = Column(String, default="medium")  # low | medium | high
    due_date = Column(Date, nullable=True)
    recurrence = Column(String, default="none")  # none | daily | weekly | monthly
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    assignee = relationship("User", foreign_keys=[assigned_to])


class Birthday(Base):
    __tablename__ = "birthdays"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    date = Column(Date, nullable=False)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    user_name = Column(String, nullable=True)
    event_type = Column(String, nullable=False)  # chore_added | chore_done | birthday_added | chat_started | memory_added
    description = Column(Text, nullable=False)
    icon = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class HousemateProfile(Base):
    __tablename__ = "housemate_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    interests = Column(Text, nullable=True)
    dietary = Column(String, nullable=True)
    tone = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    last_extracted_at = Column(DateTime, nullable=True)
    message_count_at_extraction = Column(Integer, default=0)

    user = relationship("User")


class UserStatus(Base):
    __tablename__ = "user_statuses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    status = Column(String, default="available", nullable=False)  # available | dnd
    dnd_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")


class UserMemory(Base):
    __tablename__ = "user_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    source_message_id = Column(Integer, ForeignKey("chat_messages.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    superseded = Column(Boolean, default=False)

    user = relationship("User")
