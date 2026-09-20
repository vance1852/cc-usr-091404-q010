"""FastAPI application: sample retention lifecycle system.

Endpoints
---------
Lifecycle commands (each one append-only ledger event):
  POST /samples/receive            接收
  POST /samples/aliquot            分装（父子守恒）
  POST /samples/move               库位移动
  POST /samples/checkout           领用
  POST /samples/return             归还
  POST /samples/exposure           温度暴露
  POST /holds/investigation        调查保留/解除
  POST /holds/legal                法律保留/解除
  POST /investigations             开立调查（自动引用+保留）
  POST /investigations/{id}/close  关闭调查
  POST /destruction/request        销毁申请（资格检查）
  POST /destruction/verify         双人扫码核验身份
  POST /destruction/execute        执行销毁
  POST /destruction/{id}/cancel    撤销申请
  POST /samples/{id}/correct       更正事件

Administrative queries:
  GET  /samples/{id}                          当前持有人与状态
  GET  /samples/{id}/timeline                 数量演变 + 守恒核对
  GET  /samples/{id}/references               阻断销毁的引用
  GET  /samples/{id}/corrections              完整更正轨迹
  GET  /samples/{id}/blockers                 销毁阻断项预检
  GET  /search?container_id=&batch_id=&location=
  GET  /anomalies                             待核验异常清单
  GET  /chain/verify                          全量哈希链校验
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import queries, services
from .db import get_session, init_db, make_engine
from .errors import ChainIntegrityError, DestructionBlocked, LifecycleError
from .ledger import IdempotentReplay
from .models import EventType
from .projection import rebuild_all
from .schemas import (
    AliquotIn,
    CheckoutIn,
    CorrectionIn,
    DestructionExecuteIn,
    DestructionRequestIn,
    DestructionVerifyIn,
    EventOut,
    ExposureIn,
    HoldIn,
    IdentityVerifyIn,
    InvestigationIn,
    MoveIn,
    ReceiveIn,
    ReturnIn,
)


@dataclass
class Settings:
    db_url: str


def build_settings() -> Settings:
    return Settings(db_url=os.environ.get("SAMPLE_DB_URL", "sqlite:///./sample_chain.db"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = build_settings()
    init_db(make_engine(settings.db_url))
    yield


app = FastAPI(
    title="生物样本留样生命周期系统",
    version="1.0.0",
    description="哈希链事件账本：接收/分装/移动/领用/归还/暴露/保留/销毁/更正",
    lifespan=lifespan,
)


@app.exception_handler(LifecycleError)
def _lifecycle_error_handler(request: Request, exc: LifecycleError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": exc.__class__.__name__,
                                                  "detail": str(exc)})


@app.exception_handler(DestructionBlocked)
def _blocked_handler(request: Request, exc: DestructionBlocked) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": "DestructionBlocked", "blockers": exc.blockers},
    )


@app.exception_handler(ChainIntegrityError)
def _chain_error_handler(request: Request, exc: ChainIntegrityError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "ChainIntegrityError",
                                                  "detail": exc.args[0]})


@app.exception_handler(IdempotentReplay)
def _replay_handler(request: Request, exc: IdempotentReplay) -> JSONResponse:
    ev = exc.event
    return JSONResponse(
        status_code=200,
        content={
            "idempotent_replay": True,
            "detail": "重复扫码/重复请求，返回首次事件，未产生新记录",
            "event": _event_dict(ev),
        },
    )


def _event_dict(ev) -> dict[str, Any]:
    return {
        "event_uid": ev.event_uid,
        "container_id": ev.container_id,
        "seq": ev.seq,
        "event_type": ev.event_type.value,
        "event_time": ev.event_time.isoformat(),
        "actor": ev.actor,
        "payload": ev.payload,
        "prev_hash": ev.prev_hash,
        "event_hash": ev.event_hash,
        "idempotency_key": ev.idempotency_key,
    }


def _event_response(ev) -> dict[str, Any]:
    return {"event": _event_dict(ev)}


# ------------------------------------------------------------- lifecycle ----
@app.post("/samples/receive", response_model=EventOut)
def receive(body: ReceiveIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.receive_sample(
        session,
        container_id=body.container_id,
        batch_id=body.batch_id,
        material=body.material,
        quantity=body.quantity,
        unit=body.unit,
        location=body.location,
        actor=body.actor,
        retention_until=body.retention_until,
        max_freeze_thaw=body.max_freeze_thaw,
        exposure_limit_min=body.exposure_limit_min,
        label_damaged=body.label_damaged,
        note=body.note,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/samples/aliquot")
def aliquot(body: AliquotIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    events = services.aliquot(
        session,
        parent_container_id=body.parent_container_id,
        children=[c.model_dump() for c in body.children],
        storage_location=body.storage_location,
        actor=body.actor,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return {"parent_event": _event_dict(events[0]),
            "child_events": [_event_dict(e) for e in events[1:]]}


@app.post("/samples/move", response_model=EventOut)
def move(body: MoveIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.move(
        session,
        container_id=body.container_id,
        to_location=body.to_location,
        actor=body.actor,
        label_damaged=body.label_damaged,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/samples/checkout", response_model=EventOut)
def checkout(body: CheckoutIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.checkout(
        session,
        container_id=body.container_id,
        holder=body.holder,
        quantity=body.quantity,
        purpose=body.purpose,
        actor=body.actor,
        to_location=body.to_location,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/samples/return", response_model=EventOut)
def return_sample(body: ReturnIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.return_sample(
        session,
        container_id=body.container_id,
        returned_qty=body.returned_qty,
        consumed_qty=body.consumed_qty,
        to_location=body.to_location,
        actor=body.actor,
        returned_to_frozen_storage=body.returned_to_frozen_storage,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/samples/exposure", response_model=EventOut)
def exposure(body: ExposureIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.record_temperature_exposure(
        session,
        container_id=body.container_id,
        duration_min=body.duration_min,
        max_temperature_c=body.max_temperature_c,
        crossed_freeze_thaw=body.crossed_freeze_thaw,
        actor=body.actor,
        note=body.note,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/holds/investigation", response_model=EventOut)
def investigation_hold(body: HoldIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.set_investigation_hold(
        session,
        container_id=body.container_id,
        active=body.active,
        actor=body.actor,
        reason=body.reason,
        investigation_id=body.investigation_id,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/holds/legal", response_model=EventOut)
def legal_hold(body: HoldIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ev = services.set_legal_hold(
        session,
        container_id=body.container_id,
        active=body.active,
        actor=body.actor,
        reason=body.reason,
        reference=body.reference,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/investigations")
def open_investigation(
    body: InvestigationIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    inv = services.open_investigation(
        session,
        investigation_id=body.investigation_id,
        title=body.title,
        container_ids=body.container_ids,
        actor=body.actor,
    )
    session.commit()
    return {"investigation_id": inv.investigation_id, "status": inv.status,
            "container_ids": body.container_ids}


@app.post("/investigations/{investigation_id}/close")
def close_investigation(
    investigation_id: str, actor: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    inv = services.close_investigation(
        session, investigation_id=investigation_id, actor=actor
    )
    session.commit()
    return {"investigation_id": inv.investigation_id, "status": inv.status}


# ------------------------------------------------------------ destruction ----
@app.post("/destruction/request")
def destruction_request(
    body: DestructionRequestIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    req = services.request_destruction(
        session,
        container_id=body.container_id,
        requested_by=body.requested_by,
        request_key=body.request_key,
    )
    session.commit()
    return {"request_id": req.request_id, "container_id": req.container_id,
            "status": req.status.value}


@app.post("/destruction/verify")
def destruction_verify(
    body: DestructionVerifyIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    ev = services.verify_destruction(
        session,
        request_id=body.request_id,
        verifier_a=body.verifier_a,
        verifier_b=body.verifier_b,
        scanned_code_a=body.scanned_code_a,
        scanned_code_b=body.scanned_code_b,
        identity_attested=body.identity_attested,
    )
    session.commit()
    return _event_response(ev)


@app.post("/destruction/execute")
def destruction_execute(
    body: DestructionExecuteIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    ev = services.execute_destruction(
        session, request_id=body.request_id, actor=body.actor, method=body.method
    )
    session.commit()
    return _event_response(ev)


@app.post("/destruction/{request_id}/cancel")
def destruction_cancel(
    request_id: str, actor: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    services.cancel_destruction_request(
        session, request_id=request_id, actor=actor
    )
    session.commit()
    return {"ok": True, "request_id": request_id, "status": "CANCELLED"}


# -------------------------------------------------------------- correction ----
@app.post("/samples/{container_id}/correct", response_model=EventOut)
def correct(
    container_id: str, body: CorrectionIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    ev = services.correct_event(
        session,
        container_id=container_id,
        target_seq=body.target_seq,
        actor=body.actor,
        reason=body.reason,
        corrected_event_type=EventType(body.corrected_event_type)
        if body.corrected_event_type
        else None,
        corrected_payload=body.corrected_payload,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


@app.post("/samples/{container_id}/identity-verify", response_model=EventOut)
def identity_verify(
    container_id: str, body: IdentityVerifyIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    ev = services.verify_identity(
        session,
        container_id=container_id,
        verifier_a=body.verifier_a,
        verifier_b=body.verifier_b,
        scanned_code_a=body.scanned_code_a,
        scanned_code_b=body.scanned_code_b,
        relabel=body.relabel,
        note=body.note,
        event_time=body.event_time,
        request_key=body.request_key,
    )
    session.commit()
    return _event_dict(ev)


# ----------------------------------------------------------------- queries ----
@app.get("/samples/{container_id}")
def get_sample(container_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return queries.container_status(session, container_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": f"未知容器 {container_id}"})


@app.get("/samples/{container_id}/timeline")
def get_timeline(container_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from .models import Container

    if session.get(Container, container_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"未知容器 {container_id}"})
    return queries.quantity_evolution(session, container_id)


@app.get("/samples/{container_id}/references")
def get_references(container_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from .models import Container

    if session.get(Container, container_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"未知容器 {container_id}"})
    return queries.destruction_references(session, container_id)


@app.get("/samples/{container_id}/corrections")
def get_corrections(
    container_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    from .models import Container

    if session.get(Container, container_id) is None:
        return JSONResponse(status_code=404, content={"detail": f"未知容器 {container_id}"})
    return {"container_id": container_id,
            "corrections": queries.correction_trail(session, container_id)}


@app.get("/samples/{container_id}/blockers")
def get_blockers(container_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    blockers = services.destruction_blockers(session, container_id=container_id)
    return {"container_id": container_id, "eligible": not blockers, "blockers": blockers}


@app.get("/search")
def search(
    container_id: str | None = None,
    batch_id: str | None = None,
    location: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = queries.search(
        session, container_id=container_id, batch_id=batch_id, location=location
    )
    return {"count": len(rows), "items": rows}


@app.get("/anomalies")
def anomalies(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = queries.pending_anomalies(session)
    return {"count": len(rows), "items": rows}


@app.get("/chain/verify")
def chain_verify(session: Session = Depends(get_session)) -> dict[str, Any]:
    problems = services.assert_chain_integrity(session)
    return {"ok": True, "problems": problems}


@app.post("/chain/rebuild")
def chain_rebuild(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Administrative recovery: rebuild every projection purely from the ledger."""
    rebuild_all(session)
    session.commit()
    return {"ok": True}
