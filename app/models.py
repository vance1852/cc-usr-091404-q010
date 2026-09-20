"""ORM models: append-only event ledger plus read-model projections.

The ledger (``events`` / ``event_subrefs``) is the single source of truth.
Every event on a container carries the SHA-256 of that container's previous
event, so the per-container chain cannot be broken or reordered without
detection.  Aliquot (split) events anchor child chains through ``event_subrefs``.

Projections (``containers``, ``destruction_requests`` ...) are rebuilt by
replaying the ledger; corrections are therefore *new events*, never edits.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventType(str, enum.Enum):
    RECEIVE = "RECEIVE"
    ALIQUOT = "ALIQUOT"
    MOVE = "MOVE"
    CHECKOUT = "CHECKOUT"
    RETURN = "RETURN"
    TEMP_EXPOSURE = "TEMP_EXPOSURE"
    INVESTIGATION_HOLD = "INVESTIGATION_HOLD"
    LEGAL_HOLD = "LEGAL_HOLD"
    DESTRUCTION_REQUEST = "DESTRUCTION_REQUEST"
    DESTRUCTION_VERIFY = "DESTRUCTION_VERIFY"
    DESTROY = "DESTROY"
    IDENTITY_VERIFY = "IDENTITY_VERIFY"
    CORRECTION = "CORRECTION"


class ContainerState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DESTROYED = "DESTROYED"


class RequestStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    container_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("containers.container_id"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    sub_refs: Mapped[list["EventSubRef"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        foreign_keys="EventSubRef.event_id",
    )

    __table_args__ = (
        UniqueConstraint("container_id", "seq", name="uq_event_container_seq"),
        UniqueConstraint("idempotency_key", name="uq_event_idempotency"),
        Index("ix_event_type", "event_type"),
    )


class EventSubRef(Base):
    """A reference from one ledger event to another container's chain.

    Used for aliquot lineage (parent -> child ALIQUOT events) and for the
    lifecycle of a destruction request (request -> verify -> destroy).
    """

    __tablename__ = "event_subrefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True, nullable=False)
    ref_event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    ref_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    event: Mapped[Event] = relationship(
        back_populates="sub_refs", foreign_keys=[event_id]
    )

class Container(Base):
    __tablename__ = "containers"

    container_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    material: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[ContainerState] = mapped_column(
        Enum(ContainerState), nullable=False, default=ContainerState.ACTIVE
    )
    # projection of latest chain position
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # first event's prev_hash: None for received stock, or the parent ALIQUOT
    # event hash for child aliquots — binds the child chain to its parent's.
    anchor_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContainerProjection(Base):
    """Current materialised state of one container, rebuilt by replay."""

    __tablename__ = "container_projections"

    container_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("containers.container_id"), primary_key=True
    )
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    custodian: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    checked_out_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="mL")
    freeze_thaw_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_freeze_thaw: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    cumulative_exposure_min: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exposure_limit_min: Mapped[float] = mapped_column(Float, nullable=False, default=60.0)
    usage_restricted: Mapped[str | None] = mapped_column(String(256), nullable=True)
    investigation_hold: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    legal_hold: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label_damaged: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    identity_confirmed: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    pending_identity_verification: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=0
    )
    parent_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Investigation(Base):
    __tablename__ = "investigations"

    investigation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvestigationReference(Base):
    """A stability investigation referencing a container (destruction blocker)."""

    __tablename__ = "investigation_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("investigations.investigation_id"), index=True, nullable=False
    )
    container_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("containers.container_id"), index=True, nullable=False
    )
    active: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "container_id", name="uq_investigation_container"
        ),
    )


class DestructionRequest(Base):
    __tablename__ = "destruction_requests"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    container_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("containers.container_id"), index=True, nullable=False
    )
    status: Mapped[RequestStatus] = mapped_column(
        Enum(RequestStatus), nullable=False, default=RequestStatus.PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verifier_a: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verifier_b: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
