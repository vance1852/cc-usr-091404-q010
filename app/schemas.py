"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------- requests ----
class ReceiveIn(BaseModel):
    container_id: str = Field(..., examples=["C-BULK-001"])
    batch_id: str = Field(..., examples=["B2026-09"])
    material: str = Field(..., examples=["原液-单抗X"])
    quantity: float = Field(..., gt=0)
    unit: str = "mL"
    location: str = Field(..., examples=["低温库A-架1-位3"])
    actor: str
    retention_until: datetime | None = None
    max_freeze_thaw: int = 5
    exposure_limit_min: float = 60.0
    label_damaged: bool = False
    note: str | None = None
    event_time: datetime | None = None
    request_key: str | None = Field(
        None, description="扫码请求幂等键；重复提交返回同一事件"
    )


class AliquotChild(BaseModel):
    container_id: str
    quantity: float = Field(..., gt=0)
    unit: str | None = None
    batch_id: str | None = None
    material: str | None = None
    max_freeze_thaw: int | None = None
    exposure_limit_min: float | None = None
    retention_until: datetime | None = None


class AliquotIn(BaseModel):
    parent_container_id: str
    children: list[AliquotChild]
    storage_location: str
    actor: str
    event_time: datetime | None = None
    request_key: str | None = None


class MoveIn(BaseModel):
    container_id: str
    to_location: str
    actor: str
    label_damaged: bool | None = None
    event_time: datetime | None = None
    request_key: str | None = None


class CheckoutIn(BaseModel):
    container_id: str
    holder: str = Field(..., description="实际领用人/持有人")
    quantity: float = Field(..., gt=0)
    purpose: str = Field(
        ...,
        description="用途；受限容器仅允许 INVESTIGATION / DESTRUCTION_PREP",
    )
    actor: str
    to_location: str = "使用点(未归还)"
    event_time: datetime | None = None
    request_key: str | None = None


class ReturnIn(BaseModel):
    container_id: str
    returned_qty: float = Field(..., ge=0)
    consumed_qty: float = Field(..., ge=0)
    to_location: str
    actor: str
    returned_to_frozen_storage: bool = True
    event_time: datetime | None = None
    request_key: str | None = None


class ExposureIn(BaseModel):
    container_id: str
    duration_min: float = Field(..., gt=0)
    max_temperature_c: float | None = None
    crossed_freeze_thaw: bool = False
    actor: str
    note: str | None = None
    event_time: datetime | None = None
    request_key: str | None = None


class HoldIn(BaseModel):
    container_id: str
    active: bool
    actor: str
    reason: str
    investigation_id: str | None = None
    reference: str | None = None
    event_time: datetime | None = None
    request_key: str | None = None


class InvestigationIn(BaseModel):
    investigation_id: str
    title: str
    container_ids: list[str]
    actor: str


class DestructionRequestIn(BaseModel):
    container_id: str
    requested_by: str
    request_key: str | None = None


class DestructionVerifyIn(BaseModel):
    request_id: str
    verifier_a: str
    verifier_b: str
    scanned_code_a: str = Field(..., description="核验人A扫描到的容器标识")
    scanned_code_b: str = Field(..., description="核验人B扫描到的容器标识")
    identity_attested: bool = Field(
        False, description="标签受损时须为 true：双人已比对批次记录确认物理身份"
    )


class DestructionExecuteIn(BaseModel):
    request_id: str
    actor: str
    method: str = "高压灭菌"


class IdentityVerifyIn(BaseModel):
    container_id: str
    verifier_a: str
    verifier_b: str
    scanned_code_a: str
    scanned_code_b: str
    relabel: bool = True
    note: str | None = None
    event_time: datetime | None = None
    request_key: str | None = None


class CorrectionIn(BaseModel):
    target_seq: int = Field(..., ge=1)
    actor: str
    reason: str
    corrected_event_type: Literal[
        "RECEIVE",
        "ALIQUOT",
        "MOVE",
        "CHECKOUT",
        "RETURN",
        "TEMP_EXPOSURE",
        "INVESTIGATION_HOLD",
        "LEGAL_HOLD",
        "DESTRUCTION_REQUEST",
        "DESTRUCTION_VERIFY",
        "IDENTITY_VERIFY",
    ] | None = None
    corrected_payload: dict[str, Any] | None = None
    request_key: str | None = None


# ---------------------------------------------------------------- responses ----
class EventOut(BaseModel):
    event_uid: str
    container_id: str
    seq: int
    event_type: str
    event_time: datetime
    actor: str
    payload: dict[str, Any]
    prev_hash: str | None
    event_hash: str
    idempotency_key: str | None


class BlockersOut(BaseModel):
    container_id: str
    eligible: bool
    blockers: list[dict[str, Any]]


class SimpleOk(BaseModel):
    ok: bool = True
    detail: str | None = None
