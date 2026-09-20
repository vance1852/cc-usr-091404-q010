"""查询服务：按容器 / 批次 / 库位 / 人员检索，以及审计视图。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Batch, Container, Custody, DestructionRequest, Event,
                        Investigation, InvestigationContainer, LegalHold,
                        Location, Person)
from app.services import destruction as destruction_svc
from app.services.chain import GENESIS, compute_hash, event_hash_fields
from app.services.lifecycle import (STATUS_PENDING, STATUS_RESTRICTED,
                                    effective_limits)


def person_dict(p: Person) -> dict:
    return {"code": p.code, "name": p.name, "role": p.role}


def location_dict(loc: Location) -> dict:
    return {"code": loc.code, "name": loc.name, "kind": loc.kind}


def batch_dict(b: Batch) -> dict:
    return {
        "code": b.code, "name": b.name,
        "retention_years": b.retention_years,
        "max_freeze_thaw": b.max_freeze_thaw,
        "max_exposure_minutes": b.max_exposure_minutes,
        "storage_threshold_c": b.storage_threshold_c,
    }


def holder_dict(db: Session, container: Container) -> dict:
    custody = db.get(Custody, container.id)
    if custody is None or custody.holder_type == "NONE":
        return {"type": "NONE", "code": None, "name": None}
    if custody.holder_type == "LOCATION":
        loc = db.get(Location, custody.holder_id)
        return {"type": "LOCATION", "code": loc.code, "name": loc.name}
    person = db.get(Person, custody.holder_id)
    return {"type": "PERSON", "code": person.code, "name": person.name}


def container_dict(db: Session, c: Container) -> dict:
    limits = effective_limits(c)
    return {
        "code": c.code,
        "batch_code": c.batch.code,
        "parent_code": c.parent.code if c.parent else None,
        "unit": c.unit,
        "status": c.status,
        "initial_quantity": c.initial_quantity,
        "current_quantity": c.current_quantity,
        "freeze_thaw_count": c.freeze_thaw_count,
        "max_freeze_thaw": limits["max_freeze_thaw"],
        "exposure_minutes": c.exposure_minutes,
        "max_exposure_minutes": limits["max_exposure_minutes"],
        "storage_threshold_c": limits["threshold_c"],
        "retention_until": c.retention_until.isoformat() if c.retention_until else None,
        "label_damaged": c.anomaly_type == "LABEL_DAMAGED",
        "anomaly_type": c.anomaly_type,
        "anomaly_note": c.anomaly_note,
        "holder": holder_dict(db, c),
        "destroyed_at": c.destroyed_at.isoformat() if c.destroyed_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def event_dict(db: Session, e: Event) -> dict:
    corrected_by = db.scalars(
        select(Event).where(Event.corrects_event_seq == e.seq)).first()
    return {
        "seq": e.seq,
        "event_id": e.event_id,
        "event_type": e.event_type,
        "container_code": e.container.code,
        "actor_code": e.actor.code,
        "occurred_at": e.occurred_at.isoformat(),
        "recorded_at": e.recorded_at.isoformat(),
        "payload": e.payload,
        "corrects_event_seq": e.corrects_event_seq,
        "corrected_by_seq": corrected_by.seq if corrected_by else None,
        "corrected_by_event_id": corrected_by.event_id if corrected_by else None,
        "prev_hash": e.prev_hash,
        "hash": e.hash,
    }


def container_events(db: Session, container: Container) -> list[dict]:
    events = db.scalars(select(Event).where(Event.container_id == container.id)
                        .order_by(Event.seq)).all()
    return [event_dict(db, e) for e in events]


def quantity_history(db: Session, container: Container) -> list[dict]:
    """数量演变：每个事件的变更前后数量与差值。"""
    events = db.scalars(select(Event).where(Event.container_id == container.id)
                        .order_by(Event.seq)).all()
    history = []
    for e in events:
        payload = e.payload or {}
        before = payload.get("quantity_before")
        after = payload.get("quantity_after")
        corrected_by = db.scalars(
            select(Event).where(Event.corrects_event_seq == e.seq)).first()
        history.append({
            "seq": e.seq,
            "event_id": e.event_id,
            "event_type": e.event_type,
            "actor_code": e.actor.code,
            "occurred_at": e.occurred_at.isoformat(),
            "quantity_before": before,
            "quantity_after": after,
            "delta": round(after - before, 6)
            if before is not None and after is not None else None,
            "corrected": corrected_by is not None,
            "corrected_by_event_id": corrected_by.event_id if corrected_by else None,
        })
    return history


def correction_trail(db: Session, container: Container) -> list[dict]:
    """完整更正轨迹：更正事件与被更正事件的配对。"""
    corrections = db.scalars(
        select(Event).where(Event.container_id == container.id,
                            Event.event_type == "CORRECTION").order_by(Event.seq)
    ).all()
    trail = []
    for c in corrections:
        target = db.get(Event, c.corrects_event_seq)
        trail.append({
            "correction": event_dict(db, c),
            "target": event_dict(db, target) if target else None,
            "reason": (c.payload or {}).get("reason"),
        })
    return trail


def destruction_blockers_view(db: Session, container: Container) -> dict:
    blockers = destruction_svc.compute_blockers(db, container)
    active_req = db.scalars(
        select(DestructionRequest).where(
            DestructionRequest.container_id == container.id,
            DestructionRequest.status.in_(destruction_svc.OPEN_STATUSES))
        .order_by(DestructionRequest.id.desc())
    ).first()
    return {
        "container_code": container.code,
        "blocked": bool(blockers),
        "blockers": blockers,
        "active_request": destruction_request_dict(db, active_req) if active_req else None,
    }


def destruction_request_dict(db: Session, req: DestructionRequest) -> dict:
    def pcode(pid):
        return db.get(Person, pid).code if pid else None

    return {
        "request_id": req.request_id,
        "container_code": req.container.code,
        "reason": req.reason,
        "requested_by": pcode(req.requested_by),
        "requested_at": req.requested_at.isoformat() if req.requested_at else None,
        "status": req.status,
        "blockers": req.blockers,
        "verify1_by": pcode(req.verify1_by),
        "verify1_at": req.verify1_at.isoformat() if req.verify1_at else None,
        "verify2_by": pcode(req.verify2_by),
        "verify2_at": req.verify2_at.isoformat() if req.verify2_at else None,
        "executed_by": pcode(req.executed_by),
        "executed_at": req.executed_at.isoformat() if req.executed_at else None,
        "destroy_event_seq": req.destroy_event_seq,
    }


def investigation_dict(db: Session, inv: Investigation) -> dict:
    links = db.scalars(
        select(InvestigationContainer)
        .where(InvestigationContainer.investigation_id == inv.id,
               InvestigationContainer.released_at.is_(None))
    ).all()
    return {
        "code": inv.code,
        "title": inv.title,
        "status": inv.status,
        "opened_at": inv.opened_at.isoformat() if inv.opened_at else None,
        "closed_at": inv.closed_at.isoformat() if inv.closed_at else None,
        "active_container_codes": [db.get(Container, l.container_id).code for l in links],
    }


def pending_anomalies(db: Session) -> dict:
    """待核验异常总览：待身份核验容器、超限容器、待双人核验/被阻断的销毁申请。"""
    pending_containers = db.scalars(
        select(Container).where(Container.status == STATUS_PENDING)
        .order_by(Container.code)).all()
    restricted = db.scalars(
        select(Container).where(Container.status == STATUS_RESTRICTED)
        .order_by(Container.code)).all()
    awaiting = db.scalars(
        select(DestructionRequest).where(DestructionRequest.status == "PENDING")
        .order_by(DestructionRequest.id)).all()
    blocked = db.scalars(
        select(DestructionRequest).where(DestructionRequest.status == "BLOCKED")
        .order_by(DestructionRequest.id)).all()
    return {
        "containers_pending_verification": [container_dict(db, c) for c in pending_containers],
        "restricted_containers": [container_dict(db, c) for c in restricted],
        "destruction_requests_awaiting_verification":
            [destruction_request_dict(db, r) for r in awaiting],
        "destruction_requests_blocked":
            [destruction_request_dict(db, r) for r in blocked],
    }


def verify_chain(db: Session) -> dict:
    """校验全库事件哈希链：任何篡改、插入、删除都会在此暴露。"""
    events = db.scalars(select(Event).order_by(Event.seq)).all()
    prev = GENESIS
    for e in events:
        if e.prev_hash != prev:
            return {"valid": False, "checked": e.seq, "first_bad_seq": e.seq,
                    "detail": f"事件 #{e.seq} 的前置哈希断裂"}
        if compute_hash(prev, event_hash_fields(e)) != e.hash:
            return {"valid": False, "checked": e.seq, "first_bad_seq": e.seq,
                    "detail": f"事件 #{e.seq} 的内容哈希不匹配（疑似篡改）"}
        prev = e.hash
    return {"valid": True, "checked": len(events), "first_bad_seq": None, "detail": "链完整"}
