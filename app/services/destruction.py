"""销毁工作流：法规检查 -> 双人核验容器身份 -> 执行。

阻断条件（申请时检查、执行前复检）：
- 法规保存期未满（retention_until 在未来）；
- 存在未结调查引用（OPEN 调查且引用未释放）；
- 存在有效法律保留；
- 容器存在待核验异常（如标签受损，身份未确认）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, DomainError, NotFoundError
from app.models import (Container, DestructionRequest, Investigation,
                        InvestigationContainer, LegalHold, Person)
from app.services import lifecycle
from app.services.lifecycle import STATUS_CONSUMED, STATUS_DESTROYED, STATUS_PENDING, utcnow

OPEN_STATUSES = ("PENDING", "BLOCKED", "VERIFIED")


def compute_blockers(db: Session, container: Container) -> list[dict]:
    """实时计算阻断销毁的引用清单。"""
    blockers: list[dict] = []
    today = date.today()
    if container.retention_until and today < container.retention_until:
        blockers.append({
            "type": "RETENTION_PERIOD",
            "retention_until": container.retention_until.isoformat(),
            "detail": f"法规保存期未满（{container.retention_until.isoformat()} 到期）",
        })
    links = db.scalars(
        select(InvestigationContainer)
        .join(Investigation, Investigation.id == InvestigationContainer.investigation_id)
        .where(InvestigationContainer.container_id == container.id,
               InvestigationContainer.released_at.is_(None),
               Investigation.status == "OPEN")
    ).all()
    for link in links:
        blockers.append({
            "type": "OPEN_INVESTIGATION",
            "investigation_code": link.investigation.code,
            "title": link.investigation.title,
            "detail": f"被未结调查 {link.investigation.code} 引用，销毁将破坏调查证据",
        })
    holds = db.scalars(
        select(LegalHold).where(LegalHold.container_id == container.id,
                                LegalHold.released_at.is_(None))
    ).all()
    for hold in holds:
        blockers.append({
            "type": "LEGAL_HOLD",
            "hold_id": hold.id,
            "reason": hold.reason,
            "detail": "存在有效法律保留",
        })
    if container.status == STATUS_PENDING:
        blockers.append({
            "type": "ANOMALY_OPEN",
            "anomaly_type": container.anomaly_type,
            "detail": "容器存在待核验异常（身份未确认），禁止销毁",
        })
    return blockers


def get_request(db: Session, request_id: str) -> DestructionRequest:
    req = db.scalars(select(DestructionRequest)
                     .where(DestructionRequest.request_id == request_id)).first()
    if req is None:
        raise NotFoundError(f"销毁申请不存在: {request_id}")
    return req


def create_request(db: Session, *, request_id: str, container_code: str, reason: str,
                   requested_by: str) -> tuple[DestructionRequest, bool]:
    container = lifecycle.get_container(db, container_code)
    requester = lifecycle.get_person(db, requested_by)

    existing = db.scalars(select(DestructionRequest)
                          .where(DestructionRequest.request_id == request_id)).first()
    if existing is not None:
        return existing, False  # 幂等

    if container.status == STATUS_DESTROYED:
        raise DomainError("容器已销毁", "CONTAINER_DESTROYED")
    if container.status == STATUS_CONSUMED:
        raise DomainError("容器已用尽，无实物可销毁", "CONTAINER_CONSUMED")
    open_req = db.scalars(
        select(DestructionRequest).where(
            DestructionRequest.container_id == container.id,
            DestructionRequest.status.in_(OPEN_STATUSES))
    ).first()
    if open_req is not None:
        raise ConflictError(f"容器已存在进行中的销毁申请: {open_req.request_id}",
                            "REQUEST_EXISTS")

    blockers = compute_blockers(db, container)
    req = DestructionRequest(
        request_id=request_id,
        container_id=container.id,
        reason=reason,
        requested_by=requester.id,
        requested_at=utcnow(),
        status="BLOCKED" if blockers else "PENDING",
        blockers=blockers,
    )
    db.add(req)
    db.flush()
    return req, True


def recheck(db: Session, request_id: str) -> DestructionRequest:
    """阻断引用解除后重新检查法规保存期 / 调查 / 法律保留。"""
    req = get_request(db, request_id)
    if req.status in ("EXECUTED", "CANCELLED"):
        raise ConflictError(f"申请已终结（{req.status}），不能复检", "REQUEST_CLOSED")
    blockers = compute_blockers(db, req.container)
    req.blockers = blockers
    if blockers:
        if req.status != "BLOCKED":
            req.status = "BLOCKED"
            req.verify1_by = req.verify1_at = None  # 状态倒退时核验作废
            req.verify2_by = req.verify2_at = None
    elif req.status == "BLOCKED":
        req.status = "PENDING"
    db.flush()
    return req


def verify(db: Session, request_id: str, *, person_code: str,
           scanned_container_code: str) -> DestructionRequest:
    """双人核验：两名不同人员分别现场扫描容器条码确认身份。"""
    req = get_request(db, request_id)
    person = lifecycle.get_person(db, person_code)
    if req.status == "BLOCKED":
        raise ConflictError("申请被阻断，请先解除阻断引用并复检", "REQUEST_BLOCKED",
                            details={"blockers": req.blockers})
    if req.status in ("EXECUTED", "CANCELLED"):
        raise ConflictError(f"申请已终结（{req.status}）", "REQUEST_CLOSED")
    if req.status == "VERIFIED":
        if person.id in (req.verify1_by, req.verify2_by):
            return req  # 重复提交：幂等
        raise ConflictError("申请已完成双人核验", "ALREADY_VERIFIED")

    container = req.container
    if scanned_container_code != container.code:
        raise DomainError("核验失败：现场扫描条码与申请容器不一致",
                          "CONTAINER_MISMATCH",
                          details={"expected": container.code,
                                   "scanned": scanned_container_code})

    if req.verify1_by is None:
        req.verify1_by = person.id
        req.verify1_at = utcnow()
    elif req.verify1_by == person.id:
        return req  # 同一人重复扫码：幂等，仍等待第二人
    else:
        req.verify2_by = person.id
        req.verify2_at = utcnow()
        req.status = "VERIFIED"
    db.flush()
    return req


def execute(db: Session, request_id: str, *, person_code: str,
            event_id: str) -> tuple[DestructionRequest, bool]:
    """执行销毁：仅限已完成双人核验的申请，且执行人必须是核验人之一。"""
    req = get_request(db, request_id)
    person = lifecycle.get_person(db, person_code)
    if req.status == "EXECUTED":
        return req, False  # 幂等
    if req.status != "VERIFIED":
        raise ConflictError("申请未完成双人核验，禁止执行销毁", "NOT_VERIFIED")
    if person.id not in (req.verify1_by, req.verify2_by):
        raise DomainError("执行人必须是两名核验人之一", "EXECUTOR_NOT_VERIFIER")

    # 执行前复检：核验后可能新出现阻断引用（如调查新立案）
    blockers = compute_blockers(db, req.container)
    if blockers:
        req.status = "BLOCKED"
        req.blockers = blockers
        req.verify1_by = req.verify1_at = None
        req.verify2_by = req.verify2_at = None
        # 状态回退必须落库：先提交再报错（依赖层会对异常做回滚）
        db.commit()
        raise ConflictError("执行前复检发现新的阻断引用，申请已退回阻断状态",
                            "REQUEST_BLOCKED", details={"blockers": blockers})

    ev, _ = lifecycle.destroy(
        db, event_id=event_id, container_code=req.container.code,
        actor_code=person.code, reason=req.reason,
        destruction_request_id=req.request_id)
    req.status = "EXECUTED"
    req.executed_by = person.id
    req.executed_at = utcnow()
    req.destroy_event_seq = ev.seq
    db.flush()
    return req, True


def cancel(db: Session, request_id: str, *, person_code: str) -> DestructionRequest:
    req = get_request(db, request_id)
    lifecycle.get_person(db, person_code)
    if req.status in ("EXECUTED", "CANCELLED"):
        raise ConflictError(f"申请已终结（{req.status}）", "REQUEST_CLOSED")
    req.status = "CANCELLED"
    db.flush()
    return req
