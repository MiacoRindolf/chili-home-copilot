"""Durable causal frontier for the broker-incapable captured PAPER selector."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.db import Base


class CapturedPaperSelectionFrontier(Base):
    __tablename__ = "captured_paper_selection_frontiers"
    __table_args__ = (
        UniqueConstraint(
            "account_scope",
            "expected_account_id",
            "activation_generation",
            name="uq_captured_paper_selection_frontier_generation",
        ),
        CheckConstraint(
            "account_scope = 'alpaca:paper' AND execution_family = 'alpaca_spot'",
            name="ck_captured_paper_selection_frontier_route",
        ),
        CheckConstraint(
            "status IN ('ready', 'gap')",
            name="ck_captured_paper_selection_frontier_status",
        ),
        CheckConstraint(
            "last_source_sequence >= 0 AND gap_count >= 0 "
            "AND version >= 1 AND event_sequence >= 0",
            name="ck_captured_paper_selection_frontier_counters",
        ),
    )

    id: int = Column(BigInteger, primary_key=True)
    account_scope: str = Column(String(32), nullable=False)
    expected_account_id: str = Column(String(36), nullable=False)
    activation_generation: str = Column(String(36), nullable=False)
    execution_family: str = Column(String(32), nullable=False)
    authority_sha256: str = Column(String(64), nullable=False)
    policy_sha256: str = Column(String(64), nullable=False)
    settings_projection_sha256: str = Column(String(64), nullable=False)
    code_build_sha256: str = Column(String(64), nullable=False)
    variant_set_sha256: str = Column(String(64), nullable=False)
    last_source_sequence: int = Column(BigInteger, nullable=False, default=0)
    last_source_event_at: datetime | None = Column(DateTime(timezone=True))
    last_source_available_at: datetime | None = Column(DateTime(timezone=True))
    last_batch_sha256: str | None = Column(String(64))
    status: str = Column(String(16), nullable=False, default="ready")
    gap_count: int = Column(BigInteger, nullable=False, default=0)
    version: int = Column(BigInteger, nullable=False, default=1)
    event_sequence: int = Column(BigInteger, nullable=False, default=0)
    frontier_sha256: str = Column(String(64), nullable=False)
    last_event_sha256: str | None = Column(String(64))
    created_at: datetime = Column(DateTime(timezone=True), nullable=False)
    updated_at: datetime = Column(DateTime(timezone=True), nullable=False)


class CapturedPaperSelectionFrontierEvent(Base):
    __tablename__ = "captured_paper_selection_frontier_events"
    __table_args__ = (
        UniqueConstraint(
            "frontier_id",
            "event_sequence",
            name="uq_captured_paper_selection_frontier_event_sequence",
        ),
        UniqueConstraint(
            "event_sha256",
            name="uq_captured_paper_selection_frontier_event_sha",
        ),
        CheckConstraint(
            "event_type IN ('batch_applied', 'coverage_gap')",
            name="ck_captured_paper_selection_frontier_event_type",
        ),
        CheckConstraint(
            "next_version = expected_version + 1 "
            "AND event_sequence > 0 "
            "AND source_sequence_from >= 0 "
            "AND source_sequence_through >= source_sequence_from",
            name="ck_captured_paper_selection_frontier_event_counters",
        ),
        CheckConstraint(
            "(event_type = 'batch_applied' AND batch_sha256 IS NOT NULL "
            "AND gap_sha256 IS NULL) OR "
            "(event_type = 'coverage_gap' AND batch_sha256 IS NULL "
            "AND gap_sha256 IS NOT NULL)",
            name="ck_captured_paper_selection_frontier_event_evidence",
        ),
        Index(
            "ix_captured_paper_selection_frontier_event_recorded",
            "recorded_at",
            "id",
        ),
    )

    id: int = Column(BigInteger, primary_key=True)
    frontier_id: int = Column(
        BigInteger,
        ForeignKey(
            "captured_paper_selection_frontiers.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    event_sequence: int = Column(BigInteger, nullable=False)
    event_type: str = Column(String(24), nullable=False)
    expected_version: int = Column(BigInteger, nullable=False)
    next_version: int = Column(BigInteger, nullable=False)
    expected_frontier_sha256: str = Column(String(64), nullable=False)
    next_frontier_sha256: str = Column(String(64), nullable=False)
    previous_event_sha256: str | None = Column(String(64))
    event_sha256: str = Column(String(64), nullable=False)
    batch_sha256: str | None = Column(String(64))
    gap_sha256: str | None = Column(String(64))
    source_sequence_from: int = Column(BigInteger, nullable=False)
    source_sequence_through: int = Column(BigInteger, nullable=False)
    detail_canonical_json: str = Column(Text, nullable=False)
    recorded_at: datetime = Column(DateTime(timezone=True), nullable=False)


class CapturedPaperSelectionRouteState(Base):
    """Latest sealed scoring state for one activation-bound symbol route.

    A coverage-unavailable score is a real causal result, not an absence of a
    result.  Keeping it beside the selection frontier prevents an older
    viability row from becoming executable again after a later captured input
    failed coverage.  The producer advances this row and the global frontier in
    one transaction; readers never fall back when the row is absent or stale.
    """

    __tablename__ = "captured_paper_selection_route_states"
    __table_args__ = (
        UniqueConstraint(
            "account_scope",
            "expected_account_id",
            "activation_generation",
            "symbol",
            "variant_id",
            name="uq_captured_paper_selection_route_state",
        ),
        CheckConstraint(
            "account_scope = 'alpaca:paper' AND execution_family = 'alpaca_spot'",
            name="ck_captured_paper_selection_route_state_route",
        ),
        CheckConstraint(
            "state IN ('eligible', 'coverage_unavailable')",
            name="ck_captured_paper_selection_route_state_value",
        ),
        CheckConstraint(
            "latest_source_sequence > 0 AND version > 0",
            name="ck_captured_paper_selection_route_state_counters",
        ),
        CheckConstraint(
            "source_available_at >= source_event_at AND updated_at >= created_at",
            name="ck_captured_paper_selection_route_state_clocks",
        ),
        CheckConstraint(
            "authority_sha256 ~ '^[0-9a-f]{64}$' "
            "AND evidence_sha256 ~ '^[0-9a-f]{64}$' "
            "AND batch_sha256 ~ '^[0-9a-f]{64}$' "
            "AND state_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_captured_paper_selection_route_state_hashes",
        ),
        Index(
            "ix_captured_paper_selection_route_state_symbol",
            "account_scope",
            "expected_account_id",
            "activation_generation",
            "symbol",
        ),
    )

    id: int = Column(BigInteger, primary_key=True)
    account_scope: str = Column(String(32), nullable=False)
    expected_account_id: str = Column(String(36), nullable=False)
    activation_generation: str = Column(String(36), nullable=False)
    execution_family: str = Column(String(32), nullable=False)
    authority_sha256: str = Column(String(64), nullable=False)
    symbol: str = Column(String(36), nullable=False)
    variant_id: int = Column(
        Integer,
        ForeignKey("momentum_strategy_variants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    latest_source_sequence: int = Column(BigInteger, nullable=False)
    state: str = Column(String(24), nullable=False)
    evidence_sha256: str = Column(String(64), nullable=False)
    batch_sha256: str = Column(String(64), nullable=False)
    source_event_at: datetime = Column(DateTime(timezone=True), nullable=False)
    source_available_at: datetime = Column(DateTime(timezone=True), nullable=False)
    version: int = Column(BigInteger, nullable=False, default=1)
    state_sha256: str = Column(String(64), nullable=False)
    created_at: datetime = Column(DateTime(timezone=True), nullable=False)
    updated_at: datetime = Column(DateTime(timezone=True), nullable=False)


__all__ = [
    "CapturedPaperSelectionFrontier",
    "CapturedPaperSelectionFrontierEvent",
    "CapturedPaperSelectionRouteState",
]
