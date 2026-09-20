"""Business services: lifecycle commands with conservation and custody rules.

Every state change is an :func:`ledger.append_event` followed by a projection
replay, so invariants are checked against the *derived* state and the ledger
stays the sole source of truth.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import ChainIntegrityError, DestructionBlocked, LifecycleError
from .ledger import IdempotentReplay, append_event, verify_all_chains
from .models import (
    Container,
    ContainerProjection,
    ContainerState,
    DestructionRequest,
    Event,
    EventSubRef,
    EventType,
    Investigation,
    InvestigationReference,
    RequestStatus,
    utcnow,
)
from .projection import replay_container

QTY_EPS = 1e-9

# purposes still permitted once freeze-thaw / exposure limits are exceeded
RESTRICTED_PURPOSES = {"INVESTIGATION", "DESTRUCTION_PREP"}


def _dt(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _dt(value).isoformat() if value is not None else None


def _get(session: Session, container_id: str) -> Container:
    container = session.get(Container, container_id)
    if container is None:
        raise LifecycleError(f"未知容器 {container_id!r}")
    return container


def _proj(session: Session, container_id: str) -> ContainerProjection:
    proj = session.get(ContainerProjection, container_id)
    if proj is None:
        proj = replay_container(session, container_id)
    return proj


def _require_active(container: Container) -> None:
    if container.state is ContainerState.DESTROYED:
        raise LifecycleError(f"容器 {container.container_id} 已销毁，禁止再发生保管事件")


def _append(
    session: Session,
    container_id: str,
    event_type: EventType,
    actor: str,
    payload: dict[str, Any],
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    try:
        event = append_event(
            session,
            container_id=container_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
            event_time=_dt(event_time),
            idempotency_key=request_key,
        )
    except IdempotentReplay:
        raise  # handled at API layer
    try:
        replay_container(session, container_id)
    except ValueError as exc:  # replay-level conservation violation
        raise LifecycleError(str(exc)) from exc
    return event


def _replay_if_seen(session: Session, request_key: str | None) -> None:
    """Raise IdempotentReplay before any validation when a key was consumed.

    Creation commands (receive, aliquot) would otherwise fail with a
    confusing "already exists" error on a legitimate barcode rescan.
    """
    if request_key is not None:
        existing = session.scalar(
            select(Event).where(Event.idempotency_key == request_key)
        )
        if existing is not None:
            raise IdempotentReplay(existing)


# ---------------------------------------------------------------- receive ----
def receive_sample(
    session: Session,
    *,
    container_id: str,
    batch_id: str,
    material: str,
    quantity: float,
    unit: str,
    location: str,
    actor: str,
    retention_until: datetime | None = None,
    max_freeze_thaw: int = 5,
    exposure_limit_min: float = 60.0,
    label_damaged: bool = False,
    note: str | None = None,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    if quantity <= 0:
        raise LifecycleError("接收数量必须大于 0")
    if max_freeze_thaw < 0 or exposure_limit_min < 0:
        raise LifecycleError("冻融/暴露限度不能为负")
    if session.get(Container, container_id) is not None:
        raise LifecycleError(f"容器 {container_id} 已存在，重复扫码不能再次接收")

    session.add(
        Container(
            container_id=container_id,
            batch_id=batch_id,
            material=material,
            state=ContainerState.ACTIVE,
            last_seq=0,
            created_at=_dt(event_time),
        )
    )
    session.flush()
    return _append(
        session,
        container_id,
        EventType.RECEIVE,
        actor,
        {
            "batch_id": batch_id,
            "material": material,
            "quantity": float(quantity),
            "unit": unit,
            "location": location,
            "retention_until": _iso(retention_until),
            "max_freeze_thaw": int(max_freeze_thaw),
            "exposure_limit_min": float(exposure_limit_min),
            "label_damaged": bool(label_damaged),
            "note": note,
        },
        event_time=event_time,
        request_key=request_key,
    )


# ---------------------------------------------------------------- aliquot ----
def aliquot(
    session: Session,
    *,
    parent_container_id: str,
    children: list[dict[str, Any]],
    storage_location: str,
    actor: str,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> list[Event]:
    _replay_if_seen(session, request_key)
    parent = _get(session, parent_container_id)
    _require_active(parent)
    proj = _proj(session, parent_container_id)
    if proj.checked_out_qty > QTY_EPS:
        raise LifecycleError("母容器处于领用状态，须先归还才能分装")
    if not children:
        raise LifecycleError("至少需要一个子分装")

    volumes = [float(c["quantity"]) for c in children]
    for v in volumes:
        if v <= 0:
            raise LifecycleError("子分装量必须大于 0")
    total = sum(volumes)
    if total - proj.quantity > QTY_EPS:
        raise LifecycleError(
            f"分装量不守恒：申请分装 {total:g}{proj.unit} 超过母容器余量 "
            f"{proj.quantity:g}{proj.unit}"
        )

    child_ids = [c["container_id"] for c in children]
    if len(set(child_ids)) != len(child_ids):
        raise LifecycleError("子容器标识重复")
    for cid in child_ids:
        if session.get(Container, cid) is not None:
            raise LifecycleError(f"子容器 {cid} 已存在，拒绝重复分装")

    child_payloads = []
    for c, v in zip(children, volumes):
        retention = c.get("retention_until")
        if retention is None and proj.retention_until is not None:
            retention = proj.retention_until.isoformat()
        max_ft = c.get("max_freeze_thaw")
        max_ft = int(max_ft) if max_ft is not None else proj.max_freeze_thaw
        exp_limit = c.get("exposure_limit_min")
        exp_limit = (
            float(exp_limit) if exp_limit is not None else proj.exposure_limit_min
        )
        child_payloads.append(
            {
                "container_id": c["container_id"],
                "batch_id": c.get("batch_id") or parent.batch_id,
                "material": c.get("material") or parent.material,
                "quantity": v,
                "unit": c.get("unit") or proj.unit,
                "max_freeze_thaw": max_ft,
                "exposure_limit_min": exp_limit,
                "retention_until": retention,
            }
        )

    parent_event = _append(
        session,
        parent_container_id,
        EventType.ALIQUOT,
        actor,
        {
            "children": child_payloads,
            "child_count": len(children),
            "total_volume": total,
            "remaining_volume": proj.quantity - total,
            "storage_location": storage_location,
        },
        event_time=event_time,
        request_key=request_key,
    )

    child_events: list[Event] = []
    for cp, v in zip(child_payloads, volumes):
        cid = cp["container_id"]
        session.add(
            Container(
                container_id=cid,
                batch_id=cp["batch_id"],
                material=cp["material"],
                state=ContainerState.ACTIVE,
                last_seq=0,
                anchor_event_hash=parent_event.event_hash,
                created_at=_dt(event_time),
            )
        )
        session.flush()
        child_event = _append(
            session,
            cid,
            EventType.ALIQUOT,
            actor,
            {
                "child_birth": True,
                "parent_container_id": parent_container_id,
                "parent_aliquot_event_uid": parent_event.event_uid,
                "location": storage_location,
                "quantity": v,
                "unit": cp["unit"],
                "max_freeze_thaw": cp["max_freeze_thaw"],
                "exposure_limit_min": cp["exposure_limit_min"],
                "retention_until": cp["retention_until"],
            },
            event_time=event_time,
        )
        session.add(
            EventSubRef(
                event_id=parent_event.id,
                ref_event_id=child_event.id,
                ref_kind="ALIQUOT_CHILD",
            )
        )
        child_events.append(child_event)
    return [parent_event, *child_events]


# ------------------------------------------------------------------- move ----
def move(
    session: Session,
    *,
    container_id: str,
    to_location: str,
    actor: str,
    label_damaged: bool | None = None,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    container = _get(session, container_id)
    _require_active(container)
    proj = _proj(session, container_id)
    if proj.checked_out_qty > QTY_EPS:
        raise LifecycleError(
            f"容器由 {proj.custodian} 领用中（{proj.checked_out_qty:g}{proj.unit}），"
            "归还前不得移动库位，同一时刻只能有一个有效保管位置"
        )
    if proj.location == to_location:
        raise LifecycleError("目标库位与当前库位相同")
    return _append(
        session,
        container_id,
        EventType.MOVE,
        actor,
        {
            "from_location": proj.location,
            "to_location": to_location,
            "label_damaged": label_damaged,
        },
        event_time=event_time,
        request_key=request_key,
    )


# --------------------------------------------------------------- checkout ----
def checkout(
    session: Session,
    *,
    container_id: str,
    holder: str,
    quantity: float,
    purpose: str,
    actor: str,
    to_location: str = "使用点(未归还)",
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    container = _get(session, container_id)
    _require_active(container)
    proj = _proj(session, container_id)
    if quantity <= 0:
        raise LifecycleError("领用量必须大于 0")
    if proj.checked_out_qty > QTY_EPS:
        raise LifecycleError(
            f"容器已由 {proj.custodian} 领用未归还，同一时刻只能有一个有效持有人"
        )
    if quantity - proj.quantity > QTY_EPS:
        raise LifecycleError(
            f"领用量 {quantity:g}{proj.unit} 超过在库余量 {proj.quantity:g}{proj.unit}"
        )
    if proj.usage_restricted:
        if purpose not in RESTRICTED_PURPOSES:
            raise LifecycleError(
                f"容器用途已被自动限制（{proj.usage_restricted}），"
                f"仅允许 {sorted(RESTRICTED_PURPOSES)} 用途领用"
            )
    return _append(
        session,
        container_id,
        EventType.CHECKOUT,
        actor,
        {
            "holder": holder,
            "purpose": purpose,
            "quantity": float(quantity),
            "to_location": to_location,
        },
        event_time=event_time,
        request_key=request_key,
    )


# ----------------------------------------------------------------- return ----
def return_sample(
    session: Session,
    *,
    container_id: str,
    returned_qty: float,
    consumed_qty: float,
    to_location: str,
    actor: str,
    returned_to_frozen_storage: bool = True,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    container = _get(session, container_id)
    _require_active(container)
    proj = _proj(session, container_id)
    if proj.checked_out_qty <= QTY_EPS:
        raise LifecycleError("容器当前未被领用，无法归还")
    if returned_qty < 0 or consumed_qty < 0:
        raise LifecycleError("归还量/消耗量不能为负")
    settled = returned_qty + consumed_qty
    if settled - proj.checked_out_qty > QTY_EPS:
        raise LifecycleError(
            f"数量不守恒：归还 {returned_qty:g} + 消耗 {consumed_qty:g} = "
            f"{settled:g}{proj.unit}，超过领用未还量 {proj.checked_out_qty:g}{proj.unit}"
        )
    return _append(
        session,
        container_id,
        EventType.RETURN,
        actor,
        {
            "holder": proj.custodian,
            "returned_qty": float(returned_qty),
            "consumed_qty": float(consumed_qty),
            "to_location": to_location,
            "returned_to_frozen_storage": bool(returned_to_frozen_storage),
        },
        event_time=event_time,
        request_key=request_key,
    )


# ---------------------------------------------------------- temp exposure ----
def record_temperature_exposure(
    session: Session,
    *,
    container_id: str,
    duration_min: float,
    max_temperature_c: float | None = None,
    crossed_freeze_thaw: bool = False,
    actor: str,
    note: str | None = None,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    container = _get(session, container_id)
    _require_active(container)
    if duration_min <= 0:
        raise LifecycleError("暴露时长必须大于 0")
    return _append(
        session,
        container_id,
        EventType.TEMP_EXPOSURE,
        actor,
        {
            "duration_min": float(duration_min),
            "max_temperature_c": max_temperature_c,
            "crossed_freeze_thaw": bool(crossed_freeze_thaw),
            "note": note,
        },
        event_time=event_time,
        request_key=request_key,
    )


# ------------------------------------------------------------------ holds ----
def set_investigation_hold(
    session: Session,
    *,
    container_id: str,
    active: bool,
    actor: str,
    reason: str,
    investigation_id: str | None = None,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    _require_active(_get(session, container_id))
    return _append(
        session,
        container_id,
        EventType.INVESTIGATION_HOLD,
        actor,
        {"active": bool(active), "reason": reason, "investigation_id": investigation_id},
        event_time=event_time,
        request_key=request_key,
    )


def set_legal_hold(
    session: Session,
    *,
    container_id: str,
    active: bool,
    actor: str,
    reason: str,
    reference: str | None = None,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    _replay_if_seen(session, request_key)
    _require_active(_get(session, container_id))
    return _append(
        session,
        container_id,
        EventType.LEGAL_HOLD,
        actor,
        {"active": bool(active), "reason": reason, "legal_reference": reference},
        event_time=event_time,
        request_key=request_key,
    )


# ---------------------------------------------------------- identity check ----
def verify_identity(
    session: Session,
    *,
    container_id: str,
    verifier_a: str,
    verifier_b: str,
    scanned_code_a: str,
    scanned_code_b: str,
    actor: str | None = None,
    relabel: bool = True,
    note: str | None = None,
    event_time: datetime | None = None,
    request_key: str | None = None,
) -> Event:
    """Two-person identity confirmation for a label-damaged vial.

    Both staff independently scan the vial and compare it with batch
    records.  On success the pending-verification anomaly is cleared and the
    vial may be relabelled; nothing is destroyed.
    """
    _replay_if_seen(session, request_key)
    container = _get(session, container_id)
    _require_active(container)
    if verifier_a == verifier_b:
        raise LifecycleError("身份核验必须由两名不同人员完成")
    if scanned_code_a != container_id or scanned_code_b != container_id:
        raise LifecycleError(
            "容器身份核验失败：两次扫码结果必须都与容器标识一致"
        )
    return _append(
        session,
        container_id,
        EventType.IDENTITY_VERIFY,
        verifier_a,
        {
            "verifier_a": verifier_a,
            "verifier_b": verifier_b,
            "identity_match": True,
            "relabelled": bool(relabel),
            "note": note,
        },
        event_time=event_time,
        request_key=request_key,
    )


# ---------------------------------------------------------- investigations ----
def open_investigation(
    session: Session,
    *,
    investigation_id: str,
    title: str,
    container_ids: list[str],
    actor: str,
) -> Investigation:
    if session.get(Investigation, investigation_id) is not None:
        raise LifecycleError(f"调查 {investigation_id} 已存在")
    if not container_ids:
        raise LifecycleError("调查必须至少引用一个容器")
    for cid in container_ids:
        _require_active(_get(session, cid))

    inv = Investigation(
        investigation_id=investigation_id,
        title=title,
        status="OPEN",
        opened_at=utcnow(),
    )
    session.add(inv)
    now = utcnow()
    for cid in dict.fromkeys(container_ids):
        session.add(
            InvestigationReference(
                investigation_id=investigation_id,
                container_id=cid,
                active=1,
                created_at=now,
            )
        )
        _append(
            session,
            cid,
            EventType.INVESTIGATION_HOLD,
            actor,
            {
                "active": True,
                "reason": f"调查 {investigation_id}: {title}",
                "investigation_id": investigation_id,
            },
        )
    return inv


def close_investigation(
    session: Session, *, investigation_id: str, actor: str
) -> Investigation:
    inv = session.get(Investigation, investigation_id)
    if inv is None or inv.status != "OPEN":
        raise LifecycleError(f"调查 {investigation_id} 不存在或已关闭")
    inv.status = "CLOSED"
    inv.closed_at = utcnow()

    refs = list(
        session.scalars(
            select(InvestigationReference).where(
                InvestigationReference.investigation_id == investigation_id,
                InvestigationReference.active == 1,
            )
        )
    )
    for ref in refs:
        ref.active = 0
        ref.resolved_at = utcnow()
        other_open = session.scalar(
            select(InvestigationReference.id)
            .where(
                InvestigationReference.container_id == ref.container_id,
                InvestigationReference.active == 1,
                InvestigationReference.id != ref.id,
            )
            .limit(1)
        )
        if other_open is None:
            _append(
                session,
                ref.container_id,
                EventType.INVESTIGATION_HOLD,
                actor,
                {
                    "active": False,
                    "reason": f"调查 {investigation_id} 关闭，无其他未结调查",
                    "investigation_id": investigation_id,
                },
            )
    return inv


# ------------------------------------------------------------- destruction ----
def destruction_blockers(
    session: Session, *, container_id: str, now: datetime | None = None
) -> list[dict[str, str]]:
    """Every reason destruction may not proceed. Empty list means eligible."""
    now = _dt(now)
    blockers: list[dict[str, str]] = []
    container = session.get(Container, container_id)
    if container is None:
        return [{"code": "UNKNOWN_CONTAINER", "reason": f"未知容器 {container_id}"}]
    if container.state is ContainerState.DESTROYED:
        blockers.append(
            {"code": "ALREADY_DESTROYED", "reason": "容器已经销毁"}
        )
        return blockers

    proj = _proj(session, container_id)

    open_refs = list(
        session.execute(
            select(
                InvestigationReference.investigation_id, Investigation.title
            )
            .join(Investigation, Investigation.investigation_id
                  == InvestigationReference.investigation_id)
            .where(
                InvestigationReference.container_id == container_id,
                InvestigationReference.active == 1,
            )
        )
    )
    for inv_id, title in open_refs:
        blockers.append(
            {
                "code": "OPEN_INVESTIGATION",
                "investigation_id": inv_id,
                "reason": f"被未结稳定性调查 {inv_id}（{title}）引用",
            }
        )

    if proj.legal_hold:
        blockers.append(
            {"code": "LEGAL_HOLD", "reason": "容器处于法律保留状态"}
        )

    if proj.retention_until is not None:
        retention = _dt(proj.retention_until)
        if retention > now:
            days = (retention - now).total_seconds() / 86400
            blockers.append(
                {
                    "code": "RETENTION_PERIOD",
                    "retention_until": retention.isoformat(),
                    "reason": f"法规保存期未满，还需保留 {days:.1f} 天（至 {retention.date()}）",
                }
            )

    if proj.checked_out_qty > QTY_EPS:
        blockers.append(
            {
                "code": "CHECKED_OUT",
                "reason": f"尚有 {proj.checked_out_qty:g}{proj.unit} 被 {proj.custodian} 领用未还",
            }
        )

    existing = session.scalar(
        select(DestructionRequest).where(
            DestructionRequest.container_id == container_id,
            DestructionRequest.status.in_(
                [RequestStatus.PENDING, RequestStatus.APPROVED]
            ),
        )
    )
    if existing is not None:
        blockers.append(
            {
                "code": "PENDING_REQUEST",
                "request_id": existing.request_id,
                "reason": f"已存在进行中的销毁申请 {existing.request_id}",
            }
        )
    return blockers


def request_destruction(
    session: Session,
    *,
    container_id: str,
    requested_by: str,
    request_key: str | None = None,
) -> DestructionRequest:
    _replay_if_seen(session, request_key)
    blockers = destruction_blockers(session, container_id=container_id)
    if blockers:
        raise DestructionBlocked(blockers)

    request_id = "dr_" + uuid.uuid4().hex[:16]
    event = _append(
        session,
        container_id,
        EventType.DESTRUCTION_REQUEST,
        requested_by,
        {
            "request_id": request_id,
            "eligibility_checks": [
                "法规保存期", "未结调查", "法律保留", "身份待核验", "领用未还",
            ],
            "result": "PASSED",
        },
        request_key=request_key,
    )
    req = DestructionRequest(
        request_id=request_id,
        container_id=container_id,
        status=RequestStatus.PENDING,
        requested_by=requested_by,
        requested_at=utcnow(),
        request_event_id=event.id,
    )
    session.add(req)
    session.flush()
    return req


def verify_destruction(
    session: Session,
    *,
    request_id: str,
    verifier_a: str,
    verifier_b: str,
    scanned_code_a: str,
    scanned_code_b: str,
    identity_attested: bool = False,
) -> Event:
    """Two-person verification: two distinct staff each confirm the container.

    Both scan results must equal the container id on the request.  When the
    label is damaged the staff must additionally attest that they confirmed
    the physical identity against batch records (the damaged-label anomaly
    is cleared by this attestation).
    """
    req = session.get(DestructionRequest, request_id)
    if req is None:
        raise LifecycleError(f"未知销毁申请 {request_id}")
    if req.status is not RequestStatus.PENDING:
        raise LifecycleError(f"申请状态为 {req.status.value}，不能核验")
    if verifier_a == verifier_b:
        raise LifecycleError("双人核验必须由两名不同人员完成")
    if scanned_code_a != req.container_id or scanned_code_b != req.container_id:
        raise LifecycleError(
            "容器身份核验失败：两名核验人扫码结果必须都与申请容器标识一致，"
            "已阻止销毁执行"
        )
    proj = _proj(session, req.container_id)
    if proj.label_damaged and not identity_attested:
        raise LifecycleError(
            "该容器标签受损：两名核验人须比对批次记录并明确确认物理身份"
            "（identity_attested=true）后才能通过核验"
        )

    event = _append(
        session,
        req.container_id,
        EventType.DESTRUCTION_VERIFY,
        verifier_a,
        {
            "request_id": request_id,
            "verifier_a": verifier_a,
            "verifier_b": verifier_b,
            "identity_match": True,
            "identity_attested": bool(identity_attested),
        },
    )
    session.add(
        EventSubRef(
            event_id=req.request_event_id, ref_event_id=event.id, ref_kind="VERIFY"
        )
    )
    req.status = RequestStatus.APPROVED
    req.verifier_a = verifier_a
    req.verifier_b = verifier_b
    req.verified_at = utcnow()
    session.flush()
    return event


def execute_destruction(
    session: Session, *, request_id: str, actor: str, method: str = "高压灭菌"
) -> Event:
    req = session.get(DestructionRequest, request_id)
    if req is None:
        raise LifecycleError(f"未知销毁申请 {request_id}")
    if req.status is not RequestStatus.APPROVED:
        raise LifecycleError("未经双人核验批准的销毁申请不得执行")
    blockers = destruction_blockers(session, container_id=req.container_id)
    late_blockers = [
        b for b in blockers if b["code"] not in {"PENDING_REQUEST", "ALREADY_DESTROYED"}
    ]
    if late_blockers:
        raise DestructionBlocked(late_blockers)

    event = _append(
        session,
        req.container_id,
        EventType.DESTROY,
        actor,
        {"request_id": request_id, "method": method},
    )
    session.add(
        EventSubRef(
            event_id=req.request_event_id, ref_event_id=event.id, ref_kind="DESTROY"
        )
    )
    req.status = RequestStatus.EXECUTED
    req.executed_at = utcnow()
    session.flush()
    return event


def cancel_destruction_request(session: Session, *, request_id: str, actor: str) -> None:
    req = session.get(DestructionRequest, request_id)
    if req is None:
        raise LifecycleError(f"未知销毁申请 {request_id}")
    if req.status is not RequestStatus.PENDING:
        raise LifecycleError(f"申请状态为 {req.status.value}，不能撤销")
    req.status = RequestStatus.CANCELLED
    req.reject_reason = f"由 {actor} 手动撤销"


# -------------------------------------------------------------- correction ----
def correct_event(
    session: Session,
    *,
    container_id: str,
    target_seq: int,
    actor: str,
    reason: str,
    corrected_event_type: EventType | None = None,
    corrected_payload: dict[str, Any] | None = None,
    request_key: str | None = None,
) -> Event:
    """Append a CORRECTION; the original event is preserved, never overwritten."""
    _replay_if_seen(session, request_key)
    _get(session, container_id)
    if isinstance(corrected_event_type, str):
        corrected_event_type = EventType(corrected_event_type)
    target = session.scalar(
        select(Event).where(
            Event.container_id == container_id, Event.seq == target_seq
        )
    )
    if target is None:
        raise LifecycleError(f"未找到 seq={target_seq} 的事件")
    if target.event_type in (EventType.CORRECTION, EventType.DESTROY):
        raise LifecycleError(
            f"{target.event_type.value} 事件不能被更正（销毁为不可逆物理操作；"
            "更正事件不能再被更正）"
        )
    if corrected_event_type is EventType.CORRECTION:
        raise LifecycleError("不能把事件更正为 CORRECTION")
    if not reason:
        raise LifecycleError("更正必须写明原因")

    event = _append(
        session,
        container_id,
        EventType.CORRECTION,
        actor,
        {
            "target_seq": target_seq,
            "target_event_uid": target.event_uid,
            "original_event_type": target.event_type.value,
            "original_payload": target.payload,
            "corrected_event_type": corrected_event_type.value
            if corrected_event_type
            else None,
            "corrected_payload": corrected_payload,
            "reason": reason,
        },
        request_key=request_key,
    )
    return event


# ------------------------------------------------------------- integrity ----
def assert_chain_integrity(session: Session) -> dict[str, list[str]]:
    problems = verify_all_chains(session)
    if problems:
        raise ChainIntegrityError(problems)
    return problems
