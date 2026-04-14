"""Core identity: User, Device, PairCode, BrokerCredential, BrokerSession."""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from datetime import datetime

from ..db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=True)
    google_id = Column(String, unique=True, nullable=True, index=True)
    avatar_url = Column(String, nullable=True)

    devices = relationship("Device", back_populates="user")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, nullable=False, index=True)
    label = Column(String, nullable=False)
    client_ip_last = Column(String, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="devices")


class PairCode(Base):
    __tablename__ = "pair_codes"

    code = Column(String, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)


class BrokerCredential(Base):
    __tablename__ = "broker_credentials"
    __table_args__ = (UniqueConstraint("user_id", "broker", name="uq_user_broker"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    broker = Column(String, nullable=False)
    encrypted_data = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrokerSession(Base):
    """Stores broker API session tokens in PostgreSQL so all containers
    (web, scheduler-worker, brain-worker) share a single session."""

    __tablename__ = "broker_sessions"
    __table_args__ = (UniqueConstraint("broker", "username", name="uq_broker_session"),)

    id = Column(Integer, primary_key=True)
    broker = Column(String, nullable=False, index=True)
    username = Column(String, nullable=False)
    token_data = Column(JSONB, nullable=False)
    device_token = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrainWorkerControl(Base):
    """Singleton row (id=1): cross-process wake/stop/heartbeat for brain_worker (PostgreSQL)."""

    __tablename__ = "brain_worker_control"

    id = Column(Integer, primary_key=True)
    wake_requested = Column(Boolean, nullable=False, default=False)
    stop_requested = Column(Boolean, nullable=False, default=False)
    last_heartbeat_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    last_cycle_digest_json = Column(Text, nullable=True)
    last_proposal_skips_json = Column(Text, nullable=True)
    learning_live_json = Column(Text, nullable=True)
