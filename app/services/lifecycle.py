"""生命周期核心服务：事件应用、状态重放与不变量强制。

核心思想（事件溯源）：
1. 每个业务动作先构造一个"试探事件"，与历史有效事件一起重放，
   验证守恒/状态机/保管唯一性等不变量，通过后才落库；
2. 事件落库后重放得到最新投影（Container 字段 + Custody 行）；
3. 更正（CORRECTION）也是事件：重放时跳过被更正事件、以更正载荷替代，
   历史不可变，真实状态由更正事件恢复；
4. (container_id, event_id) 唯一 + 幂等返回，抵御重复扫码。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (ConflictError, DomainError, InvariantViolation,
                        NotFoundError)
from app.models import (Batch, Container, Custody, Event, Location, Person)
from app.services.chain import GENESIS, compute_hash

EPS = 1e-6
SYSTEM_CODE = "SYSTEM"
_TENTATIVE_SEQ = 1 << 60  # 试探事件的虚拟序号，保证排在已落库事件之后

# 事件类型
RECEIVE = "RECEIVE"
ALIQUOT = "ALIQUOT"
ALIQUOT_CHILD = "ALIQUOT_CHILD"
MOVE = "MOVE"
CHECKOUT = "CHECKOUT"
RETURN = "RETURN"
TEMP_EXPOSURE = "TEMP_EXPOSURE"
FLAG_ANOMALY = "FLAG_ANOMALY"
RESOLVE_ANOMALY = "RESOLVE_ANOMALY"
DESTROY = "DESTROY"
CORRECTION = "CORRECTION"
RESTRICTION_APPLIED = "RESTRICTION_APPLIED"
RESTRICTION_LIFTED = "RESTRICTION_LIFTED"

# 允许被更正的事件类型（不产生/销毁容器、不破坏谱系）
CORRECTABLE = {MOVE, CHECKOUT, RETURN, TEMP_EXPOSURE}

STATUS_ACTIVE = "ACTIVE"
STATUS_RESTRICTED = "RESTRICTED"
STATUS_PENDING = "PENDING_VERIFICATION"
STATUS_CONSUMED = "CONSUMED"
STATUS_DESTROYED = "DESTROYED"

CHECKOUT_PURPOSES = {"ANALYSIS", "RELEASE", "INVESTIGATION", "DESTRUCTION_PREP"}
# 超限（RESTRICTED）后仅允许的用途 —— 自动限制用途
RESTRICTED_ALLOWED_PURPOSES = {"INVESTIGATION", "DESTRUCTION_PREP"}

ANOMALY_TYPES = {"LABEL_DAMAGED", "TEMP_EXCURSION", "SEAL_BROKEN", "OTHER"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def round6(x: float) -> float:
    return round(float(x), 6)


# ---------------------------------------------------------------- 基础查询

def ensure_system_actor(db: Session) -> Person:
    person = db.scalars(select(Person).where(Person.code == SYSTEM_CODE)).first()
    if person is None:
        person = Person(code=SYSTEM_CODE, name="系统", role="SYSTEM")
        db.add(person)
        db.flush()
    return person


def get_batch(db: Session, code: str) -> Batch:
    batch = db.scalars(select(Batch).where(Batch.code == code)).first()
    if batch is None:
        raise NotFoundError(f"批次不存在: {code}")
    return batch


def get_location(db: Session, code: str) -> Location:
    loc = db.scalars(select(Location).where(Location.code == code)).first()
    if loc is None:
        raise NotFoundError(f"库位不存在: {code}")
    return loc


def get_person(db: Session, code: str) -> Person:
    person = db.scalars(select(Person).where(Person.code == code)).first()
    if person is None:
        raise NotFoundError(f"人员不存在: {code}")
    return person


def get_container(db: Session, code: str) -> Container:
    container = db.scalars(select(Container).where(Container.code == code)).first()
    if container is None:
        raise NotFoundError(f"容器不存在: {code}")
    return container


def find_event(db: Session, container_id: int, event_id: str) -> Event | None:
    return db.scalars(
        select(Event).where(Event.container_id == container_id, Event.event_id == event_id)
    ).first()


# ---------------------------------------------------------------- 重放引擎

@dataclass
class DerivedState:
    """由事件流重放得到的容器真实状态。"""
    received: bool = False
    initial_quantity: float = 0.0
    quantity: float = 0.0
    holder_type: str = "NONE"          # LOCATION / PERSON / NONE
    holder_id: int | None = None
    custody_event_seq: int = 0
    freeze_thaw_count: int = 0
    exposure_minutes: float = 0.0
    anomaly_open: bool = False
    anomaly_type: str | None = None
    anomaly_note: str | None = None
    destroyed: bool = False
    out_quantity: float | None = None  # 领出时容器内数量（用于归还核销）


def effective_limits(container: Container) -> dict:
    batch = container.batch
    return {
        "max_freeze_thaw": container.max_freeze_thaw
        if container.max_freeze_thaw is not None else batch.max_freeze_thaw,
        "max_exposure_minutes": container.max_exposure_minutes
        if container.max_exposure_minutes is not None else batch.max_exposure_minutes,
        "threshold_c": batch.storage_threshold_c,
    }


def is_exceeded(freeze_thaw_count: int, exposure_minutes: float, limits: dict) -> bool:
    return (freeze_thaw_count > limits["max_freeze_thaw"]
            or exposure_minutes - limits["max_exposure_minutes"] > EPS)


def derive_status(st: DerivedState, limits: dict) -> str:
    if st.destroyed:
        return STATUS_DESTROYED
    if st.received and st.quantity <= EPS:
        return STATUS_CONSUMED
    if st.anomaly_open:
        return STATUS_PENDING
    if is_exceeded(st.freeze_thaw_count, st.exposure_minutes, limits):
        return STATUS_RESTRICTED
    return STATUS_ACTIVE


def _apply_one(etype: str, p: dict, st: DerivedState, limits: dict, seq: int) -> None:
    """把单个事件作用到派生状态；任何不变量破坏都抛 InvariantViolation。"""
    if etype in (RECEIVE, ALIQUOT_CHILD):
        if st.received:
            raise InvariantViolation("容器已存在接收/分装来源事件，不能重复建立")
        st.received = True
        st.initial_quantity = float(p["quantity"])
        st.quantity = float(p["quantity"])
        st.holder_type, st.holder_id = "LOCATION", p["location_id"]
        st.custody_event_seq = seq
    elif etype == MOVE:
        _require_received(st)
        if st.destroyed:
            raise InvariantViolation("容器已销毁，不能移动")
        st.holder_type, st.holder_id = "LOCATION", p["to_location_id"]
        st.custody_event_seq = seq
    elif etype == CHECKOUT:
        _require_received(st)
        if st.destroyed:
            raise InvariantViolation("容器已销毁，不能领用")
        if st.holder_type != "LOCATION":
            raise InvariantViolation("容器不在库位中（可能已被领用），不能重复领用")
        if st.quantity <= EPS:
            raise InvariantViolation("容器已无剩余量，不能领用")
        if st.anomaly_open:
            raise InvariantViolation("容器存在待核验异常，核验前禁止领用")
        if is_exceeded(st.freeze_thaw_count, st.exposure_minutes, limits):
            purpose = p.get("purpose", "ANALYSIS")
            if purpose not in RESTRICTED_ALLOWED_PURPOSES:
                raise InvariantViolation(
                    f"容器已超过冻融/暴露限度，用途自动受限，仅允许 {sorted(RESTRICTED_ALLOWED_PURPOSES)}")
        st.holder_type, st.holder_id = "PERSON", p["person_id"]
        st.out_quantity = st.quantity
        st.custody_event_seq = seq
    elif etype == RETURN:
        _require_received(st)
        if st.holder_type != "PERSON":
            raise InvariantViolation("容器未处于领用状态，不能归还")
        returned = float(p["returned_quantity"])
        if returned < -EPS:
            raise InvariantViolation("归还数量不能为负数")
        if returned - (st.out_quantity or 0.0) > EPS:
            raise InvariantViolation(
                f"归还数量 {returned} 超过领出数量 {st.out_quantity}，数量不守恒")
        st.quantity = max(returned, 0.0)
        st.out_quantity = None
        st.holder_type, st.holder_id = "LOCATION", p["to_location_id"]
        st.custody_event_seq = seq
    elif etype == ALIQUOT:
        _require_received(st)
        if st.destroyed:
            raise InvariantViolation("容器已销毁，不能分装")
        if st.anomaly_open:
            raise InvariantViolation("容器存在待核验异常，核验前禁止分装")
        if st.holder_type != "LOCATION":
            raise InvariantViolation("容器不在库位中（领用期间禁止分装）")
        total = sum(float(c["quantity"]) for c in p["children"])
        if total - st.quantity > EPS:
            raise InvariantViolation(
                f"子分装总量 {round6(total)} 超过母容器剩余量 {round6(st.quantity)}，数量不守恒")
        st.quantity = st.quantity - total
    elif etype == TEMP_EXPOSURE:
        _require_received(st)
        if st.destroyed:
            raise InvariantViolation("容器已销毁，不能记录暴露")
        if p.get("thawed"):
            st.freeze_thaw_count += 1
        if float(p["temperature_c"]) > limits["threshold_c"]:
            st.exposure_minutes += float(p["duration_minutes"])
    elif etype == FLAG_ANOMALY:
        _require_received(st)
        if st.destroyed:
            raise InvariantViolation("容器已销毁，不能标记异常")
        if st.anomaly_open:
            raise InvariantViolation("容器已存在待核验异常，请先完成核验")
        st.anomaly_open = True
        st.anomaly_type = p.get("anomaly_type")
        st.anomaly_note = p.get("note")
    elif etype == RESOLVE_ANOMALY:
        _require_received(st)
        if not st.anomaly_open:
            raise InvariantViolation("容器没有待核验异常")
        st.anomaly_open = False
        st.anomaly_type = None
        st.anomaly_note = None
    elif etype == DESTROY:
        _require_received(st)
        if st.destroyed:
            raise InvariantViolation("容器已销毁，不能重复销毁")
        st.destroyed = True
        st.quantity = 0.0
        st.out_quantity = None
        st.holder_type, st.holder_id = "NONE", None
        st.custody_event_seq = seq
    elif etype in (RESTRICTION_APPLIED, RESTRICTION_LIFTED):
        pass  # 系统自动标记，仅作审计轨迹
    else:
        raise InvariantViolation(f"未知事件类型: {etype}")


def _require_received(st: DerivedState) -> None:
    if not st.received:
        raise InvariantViolation("容器尚未接收，事件顺序无效")


def replay_container(db: Session, container: Container,
                     extra_events: Iterable = ()) -> DerivedState:
    """重放容器全部有效事件（被更正的事件跳过，更正事件以其修正载荷参与）。"""
    events = list(db.scalars(
        select(Event).where(Event.container_id == container.id).order_by(Event.seq)
    ).all())
    events.extend(extra_events)
    corrected_seqs = {
        e.corrects_event_seq for e in events
        if e.event_type == CORRECTION and e.corrects_event_seq
    }
    limits = effective_limits(container)
    st = DerivedState()
    for e in events:
        if e.seq in corrected_seqs:
            continue  # 被更正的事件不再生效，但保留在链上供审计
        etype, payload = e.event_type, dict(e.payload or {})
        if etype == CORRECTION:
            etype = payload["corrected_type"]
            payload = dict(payload["corrected_payload"])
        _apply_one(etype, payload, st, limits, e.seq)
    return st


# ---------------------------------------------------------------- 事件落库

def _last_hash(db: Session) -> str:
    return db.scalars(select(Event.hash).order_by(Event.seq.desc()).limit(1)).first() or GENESIS


def _append_event(db: Session, container: Container, event_type: str, actor: Person,
                  payload: dict, event_id: str, occurred_at: datetime,
                  corrects_event_seq: int | None = None) -> Event:
    prev = _last_hash(db)
    fields = {
        "event_id": event_id,
        "container_id": container.id,
        "event_type": event_type,
        "actor_id": actor.id,
        "occurred_at": occurred_at.isoformat(),
        "payload": payload,
        "corrects_event_seq": corrects_event_seq,
    }
    ev = Event(
        event_id=event_id,
        container_id=container.id,
        event_type=event_type,
        actor_id=actor.id,
        occurred_at=occurred_at,
        recorded_at=utcnow(),
        payload=payload,
        corrects_event_seq=corrects_event_seq,
        prev_hash=prev,
        hash=compute_hash(prev, fields),
    )
    db.add(ev)
    db.flush()
    return ev


def _refresh_projection(db: Session, container: Container, st: DerivedState,
                        limits: dict, ev: Event) -> None:
    container.initial_quantity = round6(st.initial_quantity)
    container.current_quantity = round6(st.quantity)
    container.freeze_thaw_count = st.freeze_thaw_count
    container.exposure_minutes = round6(st.exposure_minutes)
    container.anomaly_type = st.anomaly_type if st.anomaly_open else None
    container.anomaly_note = st.anomaly_note if st.anomaly_open else None
    container.status = derive_status(st, limits)
    if st.destroyed and container.destroyed_at is None:
        container.destroyed_at = ev.occurred_at
    custody = db.get(Custody, container.id)
    if custody is None:
        custody = Custody(container_id=container.id)
    custody.holder_type = st.holder_type
    custody.holder_id = st.holder_id
    custody.since_event_seq = st.custody_event_seq
    custody.updated_at = utcnow()
    db.add(custody)


def _append_restriction_marker(db: Session, container: Container, marker_type: str,
                               trigger: Event, st: DerivedState, limits: dict) -> None:
    system = ensure_system_actor(db)
    suffix = "#restrict" if marker_type == RESTRICTION_APPLIED else "#unrestrict"
    payload = {
        "trigger_event_id": trigger.event_id,
        "freeze_thaw_count": st.freeze_thaw_count,
        "max_freeze_thaw": limits["max_freeze_thaw"],
        "exposure_minutes": round6(st.exposure_minutes),
        "max_exposure_minutes": limits["max_exposure_minutes"],
        "reason": "冻融次数或温度暴露超过限度，用途自动受限"
        if marker_type == RESTRICTION_APPLIED else "经更正后限度恢复，用途限制解除",
        "quantity_before": round6(st.quantity),
        "quantity_after": round6(st.quantity),
    }
    _append_event(db, container, marker_type, system, payload,
                  f"{trigger.event_id}{suffix}", trigger.occurred_at)


def apply_container_event(db: Session, container: Container, event_type: str,
                          actor: Person, payload: dict, event_id: str,
                          occurred_at: datetime | None = None,
                          corrects_event_seq: int | None = None) -> tuple[Event, bool]:
    """应用一个容器事件。返回 (事件, 是否新建)；重复扫码幂等返回原事件。"""
    existing = find_event(db, container.id, event_id)
    if existing is not None:
        return existing, False

    occurred = _naive(occurred_at) or utcnow()
    limits = effective_limits(container)
    before_qty = container.current_quantity
    before_exceeded = is_exceeded(container.freeze_thaw_count,
                                  container.exposure_minutes, limits)

    # 先重放（含试探事件）验证全部不变量，通过后才真正落库
    tentative = SimpleNamespace(seq=_TENTATIVE_SEQ, event_type=event_type,
                                payload=payload, corrects_event_seq=corrects_event_seq)
    st = replay_container(db, container, extra_events=[tentative])

    full_payload = dict(payload)
    full_payload["quantity_before"] = round6(before_qty)
    full_payload["quantity_after"] = round6(st.quantity)
    ev = _append_event(db, container, event_type, actor, full_payload,
                       event_id, occurred, corrects_event_seq)
    _refresh_projection(db, container, st, limits, ev)

    after_exceeded = is_exceeded(st.freeze_thaw_count, st.exposure_minutes, limits)
    if after_exceeded and not before_exceeded:
        _append_restriction_marker(db, container, RESTRICTION_APPLIED, ev, st, limits)
    elif before_exceeded and not after_exceeded:
        _append_restriction_marker(db, container, RESTRICTION_LIFTED, ev, st, limits)
    db.flush()
    return ev, True


def _holder_snapshot(db: Session, container: Container) -> dict:
    custody = db.get(Custody, container.id)
    if custody is None or custody.holder_type == "NONE":
        return {"type": "NONE", "code": None}
    if custody.holder_type == "LOCATION":
        loc = db.get(Location, custody.holder_id)
        return {"type": "LOCATION", "code": loc.code if loc else None}
    person = db.get(Person, custody.holder_id)
    return {"type": "PERSON", "code": person.code if person else None}


# ---------------------------------------------------------------- 业务动作

def receive(db: Session, *, event_id: str, container_code: str, batch_code: str,
            quantity: float, unit: str, location_code: str, actor_code: str,
            occurred_at: datetime | None = None, retention_until: date | None = None,
            max_freeze_thaw: int | None = None,
            max_exposure_minutes: float | None = None) -> tuple[Event, bool]:
    if quantity is None or quantity <= 0:
        raise DomainError("接收数量必须为正数", "INVALID_QUANTITY")
    batch = get_batch(db, batch_code)
    location = get_location(db, location_code)
    actor = get_person(db, actor_code)

    existing = db.scalars(select(Container).where(Container.code == container_code)).first()
    if existing is not None:
        recv = db.scalars(select(Event).where(
            Event.container_id == existing.id, Event.event_type == RECEIVE)).first()
        if recv is not None and recv.event_id == event_id:
            return recv, False  # 重复扫码：幂等返回
        raise ConflictError(f"容器编码已存在: {container_code}", "CONTAINER_EXISTS")

    occurred = _naive(occurred_at) or utcnow()
    if retention_until is None:
        retention_until = occurred.date() + timedelta(days=round(batch.retention_years * 365))
    container = Container(
        code=container_code, batch_id=batch.id, unit=unit,
        initial_quantity=0.0, current_quantity=0.0,
        retention_until=retention_until,
        max_freeze_thaw=max_freeze_thaw,
        max_exposure_minutes=max_exposure_minutes,
        created_at=utcnow(),
    )
    db.add(container)
    db.flush()
    payload = {
        "quantity": quantity, "unit": unit, "batch_code": batch.code,
        "location_code": location.code, "location_id": location.id,
        "retention_until": retention_until.isoformat(),
    }
    return apply_container_event(db, container, RECEIVE, actor, payload, event_id, occurred)


def move(db: Session, *, event_id: str, container_code: str, to_location_code: str,
         actor_code: str, occurred_at: datetime | None = None) -> tuple[Event, bool]:
    container = get_container(db, container_code)
    location = get_location(db, to_location_code)
    actor = get_person(db, actor_code)
    payload = {
        "to_location_code": location.code, "to_location_id": location.id,
        "from_holder": _holder_snapshot(db, container),
    }
    return apply_container_event(db, container, MOVE, actor, payload, event_id, occurred_at)


def checkout(db: Session, *, event_id: str, container_code: str, person_code: str,
             purpose: str = "ANALYSIS", actor_code: str | None = None,
             occurred_at: datetime | None = None) -> tuple[Event, bool]:
    container = get_container(db, container_code)
    person = get_person(db, person_code)
    actor = get_person(db, actor_code or person_code)
    purpose = (purpose or "ANALYSIS").upper()
    if purpose not in CHECKOUT_PURPOSES:
        raise DomainError(f"未知领用用途: {purpose}，允许值 {sorted(CHECKOUT_PURPOSES)}",
                          "INVALID_PURPOSE")
    payload = {
        "person_code": person.code, "person_id": person.id, "purpose": purpose,
        "from_holder": _holder_snapshot(db, container),
    }
    return apply_container_event(db, container, CHECKOUT, actor, payload, event_id, occurred_at)


def return_container(db: Session, *, event_id: str, container_code: str,
                     to_location_code: str, returned_quantity: float,
                     actor_code: str, occurred_at: datetime | None = None) -> tuple[Event, bool]:
    container = get_container(db, container_code)
    location = get_location(db, to_location_code)
    actor = get_person(db, actor_code)
    if returned_quantity is None or returned_quantity < -EPS:
        raise DomainError("归还数量不能为负数", "INVALID_QUANTITY")
    consumed = round6(container.current_quantity - returned_quantity)
    payload = {
        "to_location_code": location.code, "to_location_id": location.id,
        "returned_quantity": returned_quantity,
        "consumed_quantity": consumed,  # 领用量 - 归还量 = 消耗量（守恒审计）
    }
    return apply_container_event(db, container, RETURN, actor, payload, event_id, occurred_at)


def aliquot(db: Session, *, event_id: str, parent_code: str, children: list[dict],
            actor_code: str, occurred_at: datetime | None = None) -> tuple[Event, bool]:
    parent = get_container(db, parent_code)
    actor = get_person(db, actor_code)
    existing = find_event(db, parent.id, event_id)
    if existing is not None:
        return existing, False  # 重复扫码：幂等返回，子容器不会重复创建

    if not children:
        raise DomainError("子分装列表不能为空", "EMPTY_CHILDREN")
    codes = [c["container_code"] for c in children]
    if len(set(codes)) != len(codes):
        raise DomainError("子容器编码重复", "DUPLICATE_CHILD_CODE")
    if parent_code in codes:
        raise DomainError("子容器编码不能与母容器相同", "DUPLICATE_CHILD_CODE")
    for c in children:
        if c["quantity"] is None or c["quantity"] <= 0:
            raise DomainError("子分装量必须为正数", "INVALID_QUANTITY")
        if db.scalars(select(Container).where(Container.code == c["container_code"])).first():
            raise ConflictError(f"容器编码已存在: {c['container_code']}", "CONTAINER_EXISTS")

    custody = db.get(Custody, parent.id)
    if custody is None or custody.holder_type != "LOCATION":
        raise DomainError("仅可在库位内分装（领用中的容器禁止分装）", "NOT_IN_STORAGE")
    total = sum(c["quantity"] for c in children)
    if total - parent.current_quantity > EPS:
        raise DomainError(
            f"子分装总量 {round6(total)} 超过母容器剩余量 {round6(parent.current_quantity)}",
            "CONSERVATION_VIOLATION",
            details={"parent_remaining": parent.current_quantity, "requested": total})

    occurred = _naive(occurred_at) or utcnow()
    parent_payload = {
        "children": [{"container_code": c["container_code"], "quantity": c["quantity"]}
                     for c in children],
        "total_aliquoted": round6(total),
        "location_id": custody.holder_id,
    }
    parent_ev, _ = apply_container_event(db, parent, ALIQUOT, actor, parent_payload,
                                         event_id, occurred)

    location = db.get(Location, custody.holder_id)
    for c in children:
        child = Container(
            code=c["container_code"], batch_id=parent.batch_id, parent_id=parent.id,
            unit=parent.unit, initial_quantity=0.0, current_quantity=0.0,
            retention_until=parent.retention_until,
            max_freeze_thaw=parent.max_freeze_thaw,
            max_exposure_minutes=parent.max_exposure_minutes,
            created_at=utcnow(),
        )
        db.add(child)
        db.flush()
        child_payload = {
            "parent_code": parent.code, "parent_event_id": event_id,
            "quantity": c["quantity"], "unit": parent.unit,
            "location_code": location.code if location else None,
            "location_id": custody.holder_id,
        }
        # 子容器事件标识由母事件派生：重复提交同一母事件不会重复建子样
        apply_container_event(db, child, ALIQUOT_CHILD, actor, child_payload,
                              f"{event_id}#{c['container_code']}", occurred)
    return parent_ev, True


def record_temperature(db: Session, *, event_id: str, container_code: str,
                       temperature_c: float, duration_minutes: float, thawed: bool,
                       actor_code: str, occurred_at: datetime | None = None) -> tuple[Event, bool]:
    container = get_container(db, container_code)
    actor = get_person(db, actor_code)
    if duration_minutes is None or duration_minutes <= 0:
        raise DomainError("暴露时长必须为正数", "INVALID_DURATION")
    limits = effective_limits(container)
    payload = {
        "temperature_c": temperature_c,
        "duration_minutes": duration_minutes,
        "thawed": bool(thawed),
        "threshold_c": limits["threshold_c"],
    }
    return apply_container_event(db, container, TEMP_EXPOSURE, actor, payload,
                                 event_id, occurred_at)


def flag_anomaly(db: Session, *, event_id: str, container_code: str, anomaly_type: str,
                 note: str, actor_code: str,
                 occurred_at: datetime | None = None) -> tuple[Event, bool]:
    container = get_container(db, container_code)
    actor = get_person(db, actor_code)
    anomaly_type = (anomaly_type or "").upper()
    if anomaly_type not in ANOMALY_TYPES:
        raise DomainError(f"未知异常类型: {anomaly_type}，允许值 {sorted(ANOMALY_TYPES)}",
                          "INVALID_ANOMALY_TYPE")
    payload = {"anomaly_type": anomaly_type, "note": note}
    return apply_container_event(db, container, FLAG_ANOMALY, actor, payload,
                                 event_id, occurred_at)


def resolve_anomaly(db: Session, *, event_id: str, container_code: str,
                    confirmed_container_code: str, note: str, actor_code: str,
                    occurred_at: datetime | None = None) -> tuple[Event, bool]:
    container = get_container(db, container_code)
    actor = get_person(db, actor_code)
    if confirmed_container_code != container.code:
        raise DomainError("容器身份确认失败：现场扫码与目标容器不一致",
                          "CONTAINER_MISMATCH",
                          details={"expected": container.code,
                                   "scanned": confirmed_container_code})
    payload = {"confirmed_container_code": confirmed_container_code, "note": note}
    return apply_container_event(db, container, RESOLVE_ANOMALY, actor, payload,
                                 event_id, occurred_at)


def _normalize_corrected_payload(db: Session, event_type: str, payload: dict) -> dict:
    """校验并补全更正载荷（把编码解析为稳定 id，保证重放可重现）。"""
    if event_type == MOVE:
        code = payload.get("to_location_code")
        if not code:
            raise DomainError("更正载荷缺少 to_location_code", "INVALID_CORRECTION")
        loc = get_location(db, code)
        return {"to_location_code": loc.code, "to_location_id": loc.id}
    if event_type == CHECKOUT:
        code = payload.get("person_code")
        if not code:
            raise DomainError("更正载荷缺少 person_code", "INVALID_CORRECTION")
        person = get_person(db, code)
        purpose = (payload.get("purpose") or "ANALYSIS").upper()
        if purpose not in CHECKOUT_PURPOSES:
            raise DomainError(f"未知领用用途: {purpose}", "INVALID_PURPOSE")
        return {"person_code": person.code, "person_id": person.id, "purpose": purpose}
    if event_type == RETURN:
        code = payload.get("to_location_code")
        if not code:
            raise DomainError("更正载荷缺少 to_location_code", "INVALID_CORRECTION")
        loc = get_location(db, code)
        qty = payload.get("returned_quantity")
        if qty is None or float(qty) < -EPS:
            raise DomainError("更正载荷 returned_quantity 无效", "INVALID_CORRECTION")
        return {"to_location_code": loc.code, "to_location_id": loc.id,
                "returned_quantity": float(qty)}
    if event_type == TEMP_EXPOSURE:
        duration = payload.get("duration_minutes")
        if duration is None or float(duration) <= 0:
            raise DomainError("更正载荷 duration_minutes 必须为正数", "INVALID_CORRECTION")
        return {"temperature_c": float(payload.get("temperature_c")),
                "duration_minutes": float(duration),
                "thawed": bool(payload.get("thawed"))}
    raise DomainError(f"事件类型 {event_type} 不支持更正", "NOT_CORRECTABLE")


def correct(db: Session, *, event_id: str, container_code: str, corrects_event_id: str,
            actor_code: str, reason: str, corrected_payload: dict,
            occurred_at: datetime | None = None) -> tuple[Event, bool]:
    """以更正事件恢复真实状态：原事件保留在链上但不再生效。"""
    container = get_container(db, container_code)
    actor = get_person(db, actor_code)
    existing = find_event(db, container.id, event_id)
    if existing is not None:
        return existing, False  # 重复扫码：幂等返回（优先于冲突检查）
    target = find_event(db, container.id, corrects_event_id)
    if target is None:
        raise NotFoundError(f"被更正事件不存在: {corrects_event_id}")
    if target.event_type not in CORRECTABLE:
        raise DomainError(f"事件类型 {target.event_type} 不支持更正，"
                          f"仅支持 {sorted(CORRECTABLE)}", "NOT_CORRECTABLE")
    if db.scalars(select(Event).where(Event.corrects_event_seq == target.seq)).first():
        raise ConflictError("该事件已被更正，不能重复更正", "ALREADY_CORRECTED")
    if container.status == STATUS_DESTROYED:
        raise DomainError("容器已销毁，无法更正其历史事件", "CONTAINER_DESTROYED")
    if not reason:
        raise DomainError("更正必须填写原因", "REASON_REQUIRED")

    normalized = _normalize_corrected_payload(db, target.event_type, corrected_payload or {})
    payload = {
        "corrects_event_id": target.event_id,
        "corrected_type": target.event_type,
        "corrected_payload": normalized,
        "reason": reason,
    }
    return apply_container_event(db, container, CORRECTION, actor, payload,
                                 event_id, occurred_at, corrects_event_seq=target.seq)


def destroy(db: Session, *, event_id: str, container_code: str, actor_code: str,
            reason: str, destruction_request_id: str | None = None,
            occurred_at: datetime | None = None) -> tuple[Event, bool]:
    """仅供销毁工作流调用（不暴露公开端点，强制走申请-核验-执行）。"""
    container = get_container(db, container_code)
    actor = get_person(db, actor_code)
    payload = {"reason": reason, "destruction_request_id": destruction_request_id}
    return apply_container_event(db, container, DESTROY, actor, payload,
                                 event_id, occurred_at)
