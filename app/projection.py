"""Replay projection: derive current container state from the ledger.

Projections are disposable — deleting and replaying the ledger always
reproduces them.  A CORRECTION event does not mutate history; during replay it
*replaces* the effective type/payload of the event at ``target_seq`` (the last
correction wins), so the true state is recovered while both the wrong record
and its correction remain visible in the chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    Container,
    ContainerProjection,
    ContainerState,
    Event,
    EventType,
)


@dataclass
class EffectiveEvent:
    seq: int
    event_type: EventType
    event_time: datetime
    actor: str
    payload: dict[str, Any]
    event_uid: str


def _effective_events(events: list[Event]) -> list[EffectiveEvent]:
    """Fold CORRECTION events into the events they correct."""
    corrections: dict[int, dict[str, Any]] = {}
    for ev in events:
        if ev.event_type is EventType.CORRECTION:
            corrections[ev.payload["target_seq"]] = ev.payload

    result: list[EffectiveEvent] = []
    for ev in events:
        if ev.event_type is EventType.CORRECTION:
            continue
        payload = ev.payload
        etype = ev.event_type
        correction = corrections.get(ev.seq)
        if correction is not None:
            payload = correction.get("corrected_payload", payload)
            if correction.get("corrected_event_type"):
                etype = EventType(correction["corrected_event_type"])
        result.append(
            EffectiveEvent(
                seq=ev.seq,
                event_type=etype,
                event_time=ev.event_time,
                actor=ev.actor,
                payload=payload,
                event_uid=ev.event_uid,
            )
        )
    return result


def _restriction_reasons(proj: ContainerProjection) -> list[str]:
    reasons: list[str] = []
    if proj.freeze_thaw_count > proj.max_freeze_thaw:
        reasons.append(
            f"冻融次数 {proj.freeze_thaw_count} 超过限度 {proj.max_freeze_thaw}"
        )
    if proj.cumulative_exposure_min > proj.exposure_limit_min:
        reasons.append(
            f"累计温度暴露 {proj.cumulative_exposure_min:.0f} 分钟超过限度 "
            f"{proj.exposure_limit_min:.0f} 分钟"
        )
    return reasons


def replay_container(session: Session, container_id: str) -> ContainerProjection:
    """Delete and rebuild the projection of one container from its chain."""
    container = session.get(Container, container_id)
    if container is None:
        raise ValueError(f"unknown container {container_id!r}")

    session.execute(
        delete(ContainerProjection).where(
            ContainerProjection.container_id == container_id
        )
    )

    events = list(
        session.scalars(
            select(Event)
            .where(Event.container_id == container_id)
            .order_by(Event.seq)
        )
    )
    # the ledger is authoritative: reset derived container metadata before replay
    container.state = ContainerState.ACTIVE
    if events:
        container.last_seq = events[-1].seq
        container.last_event_hash = events[-1].event_hash
    effective = _effective_events(events)

    proj = ContainerProjection(
        container_id=container_id,
        location=None,
        custodian=None,
        quantity=0.0,
        checked_out_qty=0.0,
        unit="mL",
        freeze_thaw_count=0,
        max_freeze_thaw=5,
        cumulative_exposure_min=0.0,
        exposure_limit_min=60.0,
        usage_restricted=None,
        investigation_hold=0,
        legal_hold=0,
        retention_until=None,
        label_damaged=0,
        identity_confirmed=0,
        pending_identity_verification=0,
        parent_container_id=None,
        destroyed_at=None,
    )
    session.add(proj)

    born = False
    for ev in effective:
        born = _apply_event(proj, container, ev) or born

    if not born:
        # child registered but its birth event is missing/corrected away
        proj.location = None

    proj.pending_identity_verification = int(
        proj.label_damaged and not proj.identity_confirmed
    )
    reasons = _restriction_reasons(proj)
    proj.usage_restricted = "；".join(reasons) if reasons else None
    session.flush()
    return proj


def _apply_event(
    proj: ContainerProjection, container: Container, ev: EffectiveEvent
) -> bool:
    """Apply one effective event to the projection. Returns True if it is a
    chain-birth event (RECEIVE / child ALIQUOT)."""
    p = ev.payload
    born = False

    if ev.event_type is EventType.RECEIVE:
        born = True
        proj.location = p["location"]
        proj.custodian = None
        proj.quantity = float(p["quantity"])
        proj.unit = p.get("unit", "mL")
        proj.max_freeze_thaw = int(p.get("max_freeze_thaw", 5))
        proj.exposure_limit_min = float(p.get("exposure_limit_min", 60.0))
        if p.get("retention_until"):
            proj.retention_until = _parse_dt(p["retention_until"])
        proj.label_damaged = bool(p.get("label_damaged", False))

    elif ev.event_type is EventType.ALIQUOT:
        if p.get("child_birth"):
            # first event on the child chain, anchored to parent ALIQUOT
            born = True
            proj.parent_container_id = p["parent_container_id"]
            proj.location = p["location"]
            proj.quantity = float(p["quantity"])
            proj.unit = p.get("unit", "mL")
            proj.max_freeze_thaw = int(p.get("max_freeze_thaw", 5))
            proj.exposure_limit_min = float(p.get("exposure_limit_min", 60.0))
            if p.get("retention_until"):
                proj.retention_until = _parse_dt(p["retention_until"])
        else:
            # parent: volume moved into child aliquots
            proj.quantity -= float(p["total_volume"])

    elif ev.event_type is EventType.MOVE:
        proj.location = p["to_location"]
        if p.get("label_damaged") is not None:
            proj.label_damaged = bool(p["label_damaged"])

    elif ev.event_type is EventType.CHECKOUT:
        qty = float(p.get("quantity", 0.0))
        proj.checked_out_qty += qty
        proj.quantity -= qty
        proj.custodian = p["holder"]
        proj.location = p.get("to_location", "使用点(未归还)")

    elif ev.event_type is EventType.RETURN:
        returned = float(p.get("returned_qty", 0.0))
        consumed = float(p.get("consumed_qty", 0.0))
        outstanding = proj.checked_out_qty
        if returned + consumed - outstanding > 1e-9:
            raise ValueError(
                f"{proj.container_id} seq={ev.seq}: 归还/消耗量 {returned + consumed} "
                f"超过领用量 {outstanding}"
            )
        proj.checked_out_qty = outstanding - returned - consumed
        proj.quantity += returned
        if proj.checked_out_qty <= 1e-9:
            proj.custodian = None
            proj.location = p["to_location"]
        if p.get("returned_to_frozen_storage", True):
            proj.freeze_thaw_count += 1

    elif ev.event_type is EventType.TEMP_EXPOSURE:
        proj.cumulative_exposure_min += float(p.get("duration_min", 0.0))
        if p.get("crossed_freeze_thaw"):
            proj.freeze_thaw_count += 1

    elif ev.event_type is EventType.INVESTIGATION_HOLD:
        proj.investigation_hold = bool(p["active"])

    elif ev.event_type is EventType.LEGAL_HOLD:
        proj.legal_hold = bool(p["active"])

    elif ev.event_type is EventType.DESTRUCTION_VERIFY:
        if proj.label_damaged and p.get("identity_attested"):
            proj.identity_confirmed = True

    elif ev.event_type is EventType.IDENTITY_VERIFY:
        # two-person confirmation outside the destruction workflow;
        # the vial is relabelled and the anomaly cleared
        proj.identity_confirmed = True
        if p.get("relabelled", True):
            proj.label_damaged = False

    elif ev.event_type is EventType.DESTROY:
        container.state = ContainerState.DESTROYED
        proj.destroyed_at = ev.event_time
        proj.location = "已销毁"
        proj.custodian = None
        proj.quantity = 0.0
        proj.checked_out_qty = 0.0

    # DESTRUCTION_REQUEST / DESTRUCTION_VERIFY carry no materialised state
    return born


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def rebuild_container(session: Session, container_id: str) -> ContainerProjection:
    return replay_container(session, container_id)


def rebuild_all(session: Session) -> None:
    """Recover every projection (and child container registry) from the ledger.

    Container rows are normally created by the command services, but this pass
    can also recreate child rows from parent ALIQUOT payloads, so the ledger
    alone is sufficient to restore state.
    """
    all_events = list(session.scalars(select(Event).order_by(Event.id)))
    by_container: dict[str, list[Event]] = {}
    for ev in all_events:
        by_container.setdefault(ev.container_id, []).append(ev)

    for ev in all_events:
        if ev.event_type is EventType.ALIQUOT and not ev.payload.get("child_birth"):
            anchor = ev.event_hash
            for child in ev.payload.get("children", []):
                cid = child["container_id"]
                if session.get(Container, cid) is None:
                    parent = session.get(Container, ev.container_id)
                    session.add(
                        Container(
                            container_id=cid,
                            batch_id=child.get("batch_id", parent.batch_id),
                            material=child.get("material", parent.material),
                            state=ContainerState.ACTIVE,
                            last_seq=0,
                            anchor_event_hash=anchor,
                            created_at=ev.event_time,
                        )
                    )
                    session.flush()
                by_container.setdefault(cid, [])

    for cid in list(by_container):
        replay_container(session, cid)
