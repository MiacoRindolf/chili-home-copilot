"""Chat logs, conversations, and messages."""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from ..db import Base


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    client_ip = Column(String, nullable=False)
    trace_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    action_type = Column(String, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    convo_key = Column(String, index=True, nullable=False)
    title = Column(String, default="New Chat")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="conversations")
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    convo_key = Column(String, index=True, nullable=False)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True, index=True)

    role = Column(String, nullable=False)   # "user" or "assistant"
    content = Column(Text, nullable=False)

    trace_id = Column(String, nullable=True)
    action_type = Column(String, nullable=True)
    model_used = Column(String, nullable=True)
    image_path = Column(String, nullable=True)

    conversation = relationship("Conversation", back_populates="messages")
