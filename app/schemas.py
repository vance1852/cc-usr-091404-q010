"""Pydantic 请求模型。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class BatchIn(BaseModel):
    code: str
    name: str = ""
    retention_years: float = 1.0
    max_freeze_thaw: int = 3
    max_exposure_minutes: float = 120
    storage_threshold_c: float = -20.0


class LocationIn(BaseModel):
    code: str
    name: str = ""
    kind: str = "FREEZER"


class PersonIn(BaseModel):
    code: str
    name: str
    role: str = "TECHNICIAN"


class ReceiveIn(BaseModel):
    event_id: str
    container_code: str
    batch_code: str
    quantity: float
    unit: str = "mL"
    location_code: str
    actor_code: str
    occurred_at: datetime | None = None
    retention_until: date | None = None
    max_freeze_thaw: int | None = None
    max_exposure_minutes: float | None = None


class MoveIn(BaseModel):
    event_id: str
    container_code: str
    to_location_code: str
    actor_code: str
    occurred_at: datetime | None = None


class CheckoutIn(BaseModel):
    event_id: str
    container_code: str
    person_code: str
    purpose: str = "ANALYSIS"  # ANALYSIS / RELEASE / INVESTIGATION / DESTRUCTION_PREP
    actor_code: str | None = None  # 记录人，缺省等于领用人
    occurred_at: datetime | None = None


class ReturnIn(BaseModel):
    event_id: str
    container_code: str
    to_location_code: str
    returned_quantity: float
    actor_code: str
    occurred_at: datetime | None = None


class AliquotChildIn(BaseModel):
    container_code: str
    quantity: float


class AliquotIn(BaseModel):
    event_id: str
    parent_code: str
    children: list[AliquotChildIn]
    actor_code: str
    occurred_at: datetime | None = None


class TemperatureIn(BaseModel):
    event_id: str
    container_code: str
    temperature_c: float
    duration_minutes: float
    thawed: bool = False  # 是否构成一次冻融
    actor_code: str
    occurred_at: datetime | None = None


class FlagAnomalyIn(BaseModel):
    event_id: str
    container_code: str
    anomaly_type: str  # LABEL_DAMAGED / TEMP_EXCURSION / SEAL_BROKEN / OTHER
    note: str = ""
    actor_code: str
    occurred_at: datetime | None = None


class ResolveAnomalyIn(BaseModel):
    event_id: str
    container_code: str
    confirmed_container_code: str  # 重新扫码确认容器身份
    note: str = ""
    actor_code: str
    occurred_at: datetime | None = None


class CorrectionIn(BaseModel):
    event_id: str
    container_code: str
    corrects_event_id: str  # 被更正事件的客户端事件标识
    actor_code: str
    reason: str
    corrected_payload: dict  # 该事件类型的正确取值
    occurred_at: datetime | None = None


class InvestigationIn(BaseModel):
    code: str
    title: str = ""


class LinkContainerIn(BaseModel):
    container_code: str


class LegalHoldIn(BaseModel):
    reason: str
    person_code: str


class PersonActionIn(BaseModel):
    person_code: str


class DestructionRequestIn(BaseModel):
    request_id: str
    container_code: str
    reason: str = ""
    requested_by: str


class VerifyIn(BaseModel):
    person_code: str
    scanned_container_code: str  # 核验时现场扫描的容器条码


class ExecuteIn(BaseModel):
    person_code: str
    event_id: str  # 销毁事件的幂等键
