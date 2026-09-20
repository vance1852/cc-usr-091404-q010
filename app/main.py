"""FastAPI 入口：生物样本留样生命周期系统。

事件写接口统一返回 (事件, 是否新建)：重复扫码（同容器同事件标识）返回 200 与原事件，
新事件返回 201。所有查询接口只读投影与事件链，不写库。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db, init_db
from app.errors import DomainError
from app.models import (Batch, Container, Custody, Event, Investigation,
                        InvestigationContainer, LegalHold, Location, Person)
from app.services import destruction as destruction_svc
from app.services import lifecycle, queries
from app.services.lifecycle import utcnow


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="生物样本留样生命周期系统", version="1.0.0", lifespan=lifespan)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


def _event_response(db: Session, result: tuple, response: Response) -> dict:
    ev, created = result
    response.status_code = 201 if created else 200
    return queries.event_dict(db, ev)


# ------------------------------------------------------------------ 基础档案

@app.post("/batches", status_code=201)
def create_batch(body: schemas.BatchIn, db: Session = Depends(get_db)):
    if db.scalars(select(Batch).where(Batch.code == body.code)).first():
        raise DomainError(f"批次已存在: {body.code}", "BATCH_EXISTS", status_code=409)
    batch = Batch(code=body.code, name=body.name,
                  retention_years=body.retention_years,
                  max_freeze_thaw=body.max_freeze_thaw,
                  max_exposure_minutes=body.max_exposure_minutes,
                  storage_threshold_c=body.storage_threshold_c)
    db.add(batch)
    db.flush()
    return queries.batch_dict(batch)


@app.get("/batches")
def list_batches(db: Session = Depends(get_db)):
    return [queries.batch_dict(b) for b in db.scalars(select(Batch).order_by(Batch.code))]


@app.post("/locations", status_code=201)
def create_location(body: schemas.LocationIn, db: Session = Depends(get_db)):
    if db.scalars(select(Location).where(Location.code == body.code)).first():
        raise DomainError(f"库位已存在: {body.code}", "LOCATION_EXISTS", status_code=409)
    loc = Location(code=body.code, name=body.name, kind=body.kind)
    db.add(loc)
    db.flush()
    return queries.location_dict(loc)


@app.get("/locations")
def list_locations(db: Session = Depends(get_db)):
    return [queries.location_dict(l) for l in db.scalars(select(Location).order_by(Location.code))]


@app.post("/persons", status_code=201)
def create_person(body: schemas.PersonIn, db: Session = Depends(get_db)):
    if db.scalars(select(Person).where(Person.code == body.code)).first():
        raise DomainError(f"人员已存在: {body.code}", "PERSON_EXISTS", status_code=409)
    person = Person(code=body.code, name=body.name, role=body.role)
    db.add(person)
    db.flush()
    return queries.person_dict(person)


@app.get("/persons")
def list_persons(db: Session = Depends(get_db)):
    return [queries.person_dict(p) for p in db.scalars(select(Person).order_by(Person.code))]


# ------------------------------------------------------------------ 事件写入

@app.post("/containers/receive")
def receive_container(body: schemas.ReceiveIn, response: Response,
                      db: Session = Depends(get_db)):
    result = lifecycle.receive(
        db, event_id=body.event_id, container_code=body.container_code,
        batch_code=body.batch_code, quantity=body.quantity, unit=body.unit,
        location_code=body.location_code, actor_code=body.actor_code,
        occurred_at=body.occurred_at, retention_until=body.retention_until,
        max_freeze_thaw=body.max_freeze_thaw,
        max_exposure_minutes=body.max_exposure_minutes)
    return _event_response(db, result, response)


@app.post("/events/move")
def move_container(body: schemas.MoveIn, response: Response,
                   db: Session = Depends(get_db)):
    result = lifecycle.move(db, event_id=body.event_id,
                            container_code=body.container_code,
                            to_location_code=body.to_location_code,
                            actor_code=body.actor_code, occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/checkout")
def checkout_container(body: schemas.CheckoutIn, response: Response,
                       db: Session = Depends(get_db)):
    result = lifecycle.checkout(db, event_id=body.event_id,
                                container_code=body.container_code,
                                person_code=body.person_code, purpose=body.purpose,
                                actor_code=body.actor_code, occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/return")
def return_container(body: schemas.ReturnIn, response: Response,
                     db: Session = Depends(get_db)):
    result = lifecycle.return_container(db, event_id=body.event_id,
                                        container_code=body.container_code,
                                        to_location_code=body.to_location_code,
                                        returned_quantity=body.returned_quantity,
                                        actor_code=body.actor_code,
                                        occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/aliquot")
def aliquot_container(body: schemas.AliquotIn, response: Response,
                      db: Session = Depends(get_db)):
    children = [c.model_dump() for c in body.children]
    result = lifecycle.aliquot(db, event_id=body.event_id, parent_code=body.parent_code,
                               children=children, actor_code=body.actor_code,
                               occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/temperature")
def temperature_event(body: schemas.TemperatureIn, response: Response,
                      db: Session = Depends(get_db)):
    result = lifecycle.record_temperature(
        db, event_id=body.event_id, container_code=body.container_code,
        temperature_c=body.temperature_c, duration_minutes=body.duration_minutes,
        thawed=body.thawed, actor_code=body.actor_code, occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/flag-anomaly")
def flag_anomaly(body: schemas.FlagAnomalyIn, response: Response,
                 db: Session = Depends(get_db)):
    result = lifecycle.flag_anomaly(db, event_id=body.event_id,
                                    container_code=body.container_code,
                                    anomaly_type=body.anomaly_type, note=body.note,
                                    actor_code=body.actor_code,
                                    occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/resolve-anomaly")
def resolve_anomaly(body: schemas.ResolveAnomalyIn, response: Response,
                    db: Session = Depends(get_db)):
    result = lifecycle.resolve_anomaly(
        db, event_id=body.event_id, container_code=body.container_code,
        confirmed_container_code=body.confirmed_container_code, note=body.note,
        actor_code=body.actor_code, occurred_at=body.occurred_at)
    return _event_response(db, result, response)


@app.post("/events/corrections")
def correct_event(body: schemas.CorrectionIn, response: Response,
                  db: Session = Depends(get_db)):
    result = lifecycle.correct(db, event_id=body.event_id,
                               container_code=body.container_code,
                               corrects_event_id=body.corrects_event_id,
                               actor_code=body.actor_code, reason=body.reason,
                               corrected_payload=body.corrected_payload,
                               occurred_at=body.occurred_at)
    return _event_response(db, result, response)


# ------------------------------------------------------------------ 容器查询

@app.get("/containers")
def list_containers(batch_code: str | None = None, location_code: str | None = None,
                    person_code: str | None = None, status: str | None = None,
                    db: Session = Depends(get_db)):
    stmt = select(Container).order_by(Container.code)
    if batch_code:
        stmt = stmt.join(Batch, Batch.id == Container.batch_id).where(Batch.code == batch_code)
    if status:
        stmt = stmt.where(Container.status == status)
    containers = list(db.scalars(stmt).all())
    if location_code:
        loc = lifecycle.get_location(db, location_code)
        containers = [c for c in containers
                      if (cs := db.get(Custody, c.id)) and cs.holder_type == "LOCATION"
                      and cs.holder_id == loc.id]
    if person_code:
        person = lifecycle.get_person(db, person_code)
        containers = [c for c in containers
                      if (cs := db.get(Custody, c.id)) and cs.holder_type == "PERSON"
                      and cs.holder_id == person.id]
    return [queries.container_dict(db, c) for c in containers]


@app.get("/containers/{code}")
def get_container(code: str, db: Session = Depends(get_db)):
    return queries.container_dict(db, lifecycle.get_container(db, code))


@app.get("/containers/{code}/events")
def get_container_events(code: str, db: Session = Depends(get_db)):
    container = lifecycle.get_container(db, code)
    return queries.container_events(db, container)


@app.get("/containers/{code}/quantity-history")
def get_quantity_history(code: str, db: Session = Depends(get_db)):
    container = lifecycle.get_container(db, code)
    return queries.quantity_history(db, container)


@app.get("/containers/{code}/destruction-blockers")
def get_destruction_blockers(code: str, db: Session = Depends(get_db)):
    container = lifecycle.get_container(db, code)
    return queries.destruction_blockers_view(db, container)


@app.get("/containers/{code}/corrections")
def get_corrections(code: str, db: Session = Depends(get_db)):
    container = lifecycle.get_container(db, code)
    return queries.correction_trail(db, container)


# ------------------------------------------------------------------ 维度查询

@app.get("/batches/{code}/containers")
def batch_containers(code: str, db: Session = Depends(get_db)):
    batch = lifecycle.get_batch(db, code)
    containers = db.scalars(select(Container).where(Container.batch_id == batch.id)
                            .order_by(Container.code)).all()
    return [queries.container_dict(db, c) for c in containers]


@app.get("/locations/{code}/containers")
def location_containers(code: str, db: Session = Depends(get_db)):
    loc = lifecycle.get_location(db, code)
    rows = db.scalars(select(Custody).where(Custody.holder_type == "LOCATION",
                                            Custody.holder_id == loc.id)).all()
    return [queries.container_dict(db, db.get(Container, r.container_id)) for r in rows]


@app.get("/persons/{code}/containers")
def person_containers(code: str, db: Session = Depends(get_db)):
    person = lifecycle.get_person(db, code)
    rows = db.scalars(select(Custody).where(Custody.holder_type == "PERSON",
                                            Custody.holder_id == person.id)).all()
    return [queries.container_dict(db, db.get(Container, r.container_id)) for r in rows]


@app.get("/anomalies/pending")
def anomalies_pending(db: Session = Depends(get_db)):
    return queries.pending_anomalies(db)


@app.get("/events/verify-chain")
def verify_chain(db: Session = Depends(get_db)):
    return queries.verify_chain(db)


# ------------------------------------------------------------------ 调查与法律保留

@app.post("/investigations", status_code=201)
def create_investigation(body: schemas.InvestigationIn, db: Session = Depends(get_db)):
    if db.scalars(select(Investigation).where(Investigation.code == body.code)).first():
        raise DomainError(f"调查已存在: {body.code}", "INVESTIGATION_EXISTS", status_code=409)
    inv = Investigation(code=body.code, title=body.title, status="OPEN",
                        opened_at=utcnow())
    db.add(inv)
    db.flush()
    return queries.investigation_dict(db, inv)


@app.get("/investigations")
def list_investigations(db: Session = Depends(get_db)):
    invs = db.scalars(select(Investigation).order_by(Investigation.code)).all()
    return [queries.investigation_dict(db, i) for i in invs]


@app.post("/investigations/{code}/containers", status_code=201)
def link_investigation_container(code: str, body: schemas.LinkContainerIn,
                                 response: Response, db: Session = Depends(get_db)):
    inv = db.scalars(select(Investigation).where(Investigation.code == code)).first()
    if inv is None:
        raise DomainError(f"调查不存在: {code}", "NOT_FOUND", status_code=404)
    if inv.status != "OPEN":
        raise DomainError("调查已结案，不能新增引用", "INVESTIGATION_CLOSED",
                          status_code=409)
    container = lifecycle.get_container(db, body.container_code)
    link = db.scalars(select(InvestigationContainer).where(
        InvestigationContainer.investigation_id == inv.id,
        InvestigationContainer.container_id == container.id)).first()
    if link is not None:
        if link.released_at is None:
            response.status_code = 200  # 重复引用：幂等
            return queries.investigation_dict(db, inv)
        link.released_at = None  # 曾释放：重新建立引用
    else:
        db.add(InvestigationContainer(investigation_id=inv.id,
                                      container_id=container.id,
                                      linked_at=utcnow()))
    db.flush()
    return queries.investigation_dict(db, inv)


@app.post("/investigations/{code}/containers/{container_code}/release")
def release_investigation_container(code: str, container_code: str,
                                    db: Session = Depends(get_db)):
    inv = db.scalars(select(Investigation).where(Investigation.code == code)).first()
    if inv is None:
        raise DomainError(f"调查不存在: {code}", "NOT_FOUND", status_code=404)
    container = lifecycle.get_container(db, container_code)
    link = db.scalars(select(InvestigationContainer).where(
        InvestigationContainer.investigation_id == inv.id,
        InvestigationContainer.container_id == container.id,
        InvestigationContainer.released_at.is_(None))).first()
    if link is None:
        raise DomainError("该容器不在此调查的引用中", "NOT_FOUND", status_code=404)
    link.released_at = utcnow()
    db.flush()
    return queries.investigation_dict(db, inv)


@app.post("/investigations/{code}/close")
def close_investigation(code: str, db: Session = Depends(get_db)):
    inv = db.scalars(select(Investigation).where(Investigation.code == code)).first()
    if inv is None:
        raise DomainError(f"调查不存在: {code}", "NOT_FOUND", status_code=404)
    if inv.status == "OPEN":
        inv.status = "CLOSED"
        inv.closed_at = utcnow()
        db.flush()
    return queries.investigation_dict(db, inv)


@app.post("/containers/{code}/legal-holds", status_code=201)
def place_legal_hold(code: str, body: schemas.LegalHoldIn, db: Session = Depends(get_db)):
    container = lifecycle.get_container(db, code)
    person = lifecycle.get_person(db, body.person_code)
    active = db.scalars(select(LegalHold).where(
        LegalHold.container_id == container.id, LegalHold.released_at.is_(None))).first()
    if active is not None:
        raise DomainError("容器已存在有效法律保留", "HOLD_EXISTS", status_code=409)
    hold = LegalHold(container_id=container.id, reason=body.reason,
                     placed_by=person.id, placed_at=utcnow())
    db.add(hold)
    db.flush()
    return {"id": hold.id, "container_code": container.code, "reason": hold.reason,
            "placed_by": person.code, "placed_at": hold.placed_at.isoformat(),
            "released_at": None}


@app.post("/legal-holds/{hold_id}/release")
def release_legal_hold(hold_id: int, body: schemas.PersonActionIn,
                       db: Session = Depends(get_db)):
    hold = db.get(LegalHold, hold_id)
    if hold is None:
        raise DomainError(f"法律保留不存在: {hold_id}", "NOT_FOUND", status_code=404)
    person = lifecycle.get_person(db, body.person_code)
    if hold.released_at is None:
        hold.released_at = utcnow()
        hold.released_by = person.id
        db.flush()
    return {"id": hold.id, "container_code": db.get(Container, hold.container_id).code,
            "reason": hold.reason, "released_at": hold.released_at.isoformat(),
            "released_by": person.code}


# ------------------------------------------------------------------ 销毁工作流

@app.post("/destruction-requests")
def create_destruction_request(body: schemas.DestructionRequestIn, response: Response,
                               db: Session = Depends(get_db)):
    req, created = destruction_svc.create_request(
        db, request_id=body.request_id, container_code=body.container_code,
        reason=body.reason, requested_by=body.requested_by)
    response.status_code = 201 if created else 200
    return queries.destruction_request_dict(db, req)


@app.get("/destruction-requests/{request_id}")
def get_destruction_request(request_id: str, db: Session = Depends(get_db)):
    return queries.destruction_request_dict(db, destruction_svc.get_request(db, request_id))


@app.post("/destruction-requests/{request_id}/recheck")
def recheck_destruction_request(request_id: str, db: Session = Depends(get_db)):
    return queries.destruction_request_dict(db, destruction_svc.recheck(db, request_id))


@app.post("/destruction-requests/{request_id}/verify")
def verify_destruction_request(request_id: str, body: schemas.VerifyIn,
                               db: Session = Depends(get_db)):
    req = destruction_svc.verify(db, request_id, person_code=body.person_code,
                                 scanned_container_code=body.scanned_container_code)
    return queries.destruction_request_dict(db, req)


@app.post("/destruction-requests/{request_id}/execute")
def execute_destruction_request(request_id: str, body: schemas.ExecuteIn,
                                response: Response, db: Session = Depends(get_db)):
    req, executed = destruction_svc.execute(db, request_id,
                                            person_code=body.person_code,
                                            event_id=body.event_id)
    response.status_code = 201 if executed else 200
    return queries.destruction_request_dict(db, req)


@app.post("/destruction-requests/{request_id}/cancel")
def cancel_destruction_request(request_id: str, body: schemas.PersonActionIn,
                               db: Session = Depends(get_db)):
    req = destruction_svc.cancel(db, request_id, person_code=body.person_code)
    return queries.destruction_request_dict(db, req)
