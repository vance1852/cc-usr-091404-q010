"""Read-side queries for administrators.

All answers are derived from the ledger + projections: current holder,
quantity evolution, destruction-blocking references, anomalies awaiting
identity verification and the full correction trail.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Container,
    ContainerProjection,
    ContainerState,
    DestructionRequest,
    Event,
    EventType,
    Investigation,
    InvestigationReference,
)
from .projection import EffectiveEvent, _effective_events
from .services import destruction_blockers

# event types that change on-hand / checked-out quantities
_QTY_EVENTS = {
    EventType.RECEIVE,
    EventType.ALIQUOT,
    EventType.CHECKOUT,
    EventType.RETURN,
    EventType.DESTROY,
}


def _load_events(session: Session, container_id: str) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(Event.container_id == container_id)
            .order_by(Event.seq)
        )
    )


def container_status(session: Session, container_id: str) -> dict[str, Any]:
    container = session.get(Container, container_id)
    if container is None:
        raise KeyError(container_id)
    proj = session.get(ContainerProjection, container_id)
    return {
        "container_id": container.container_id,
        "batch_id": container.batch_id,
        "material": container.material,
        "state": container.state.value,
        "location": proj.location if proj else None,
        "custodian": proj.custodian if proj else None,
        "on_hand_qty": proj.quantity if proj else 0.0,
        "checked_out_qty": proj.checked_out_qty if proj else 0.0,
        "unit": proj.unit if proj else None,
        "freeze_thaw_count": proj.freeze_thaw_count if proj else 0,
        "max_freeze_thaw": proj.max_freeze_thaw if proj else None,
        "cumulative_exposure_min": proj.cumulative_exposure_min if proj else 0.0,
        "exposure_limit_min": proj.exposure_limit_min if proj else None,
        "usage_restricted": proj.usage_restricted if proj else None,
        "investigation_hold": bool(proj.investigation_hold) if proj else False,
        "legal_hold": bool(proj.legal_hold) if proj else False,
        "label_damaged": bool(proj.label_damaged) if proj else False,
        "pending_identity_verification": bool(
            proj.pending_identity_verification
        )
        if proj
        else False,
        "retention_until": proj.retention_until.isoformat()
        if proj and proj.retention_until
        else None,
        "parent_container_id": proj.parent_container_id if proj else None,
        "destroyed_at": proj.destroyed_at.isoformat()
        if proj and proj.destroyed_at
        else None,
        "last_seq": container.last_seq,
        "last_event_hash": container.last_event_hash,
    }


def _timeline_entry(
    ev: EffectiveEvent, *, corrected: bool, correction_of: Event | None
) -> dict[str, Any]:
    p = ev.payload
    delta_on_hand: float = 0.0
    delta_checked_out: float = 0.0
    detail: dict[str, Any] = {}

    if ev.event_type is EventType.RECEIVE:
        delta_on_hand = float(p["quantity"])
        detail = {"location": p["location"]}
    elif ev.event_type is EventType.ALIQUOT:
        if p.get("child_birth"):
            delta_on_hand = float(p["quantity"])
            detail = {"parent_container_id": p["parent_container_id"]}
        else:
            delta_on_hand = -float(p["total_volume"])
            detail = {
                "children": [c["container_id"] for c in p.get("children", [])],
                "remaining_volume": p.get("remaining_volume"),
            }
    elif ev.event_type is EventType.CHECKOUT:
        q = float(p["quantity"])
        delta_on_hand, delta_checked_out = -q, q
        detail = {"holder": p["holder"], "purpose": p.get("purpose")}
    elif ev.event_type is EventType.RETURN:
        r, c = float(p["returned_qty"]), float(p["consumed_qty"])
        delta_on_hand, delta_checked_out = r, -(r + c)
        detail = {"consumed_qty": c, "to_location": p["to_location"]}
    elif ev.event_type is EventType.TEMP_EXPOSURE:
        detail = {
            "duration_min": p["duration_min"],
            "crossed_freeze_thaw": p.get("crossed_freeze_thaw", False),
        }
    elif ev.event_type is EventType.MOVE:
        detail = {"from_location": p.get("from_location"), "to_location": p["to_location"]}
    elif ev.event_type is EventType.INVESTIGATION_HOLD:
        detail = {"active": p["active"], "investigation_id": p.get("investigation_id")}
    elif ev.event_type is EventType.LEGAL_HOLD:
        detail = {"active": p["active"], "legal_reference": p.get("legal_reference")}
    elif ev.event_type is EventType.DESTRUCTION_REQUEST:
        detail = {"request_id": p.get("request_id"), "result": p.get("result")}
    elif ev.event_type is EventType.DESTRUCTION_VERIFY:
        detail = {"verifier_a": p.get("verifier_a"), "verifier_b": p.get("verifier_b")}
    elif ev.event_type is EventType.IDENTITY_VERIFY:
        detail = {
            "verifier_a": p.get("verifier_a"),
            "verifier_b": p.get("verifier_b"),
            "relabelled": p.get("relabelled"),
        }
    elif ev.event_type is EventType.DESTROY:
        detail = {"method": p.get("method"), "request_id": p.get("request_id")}

    entry = {
        "seq": ev.seq,
        "event_uid": ev.event_uid,
        "event_type": ev.event_type.value,
        "event_time": ev.event_time.isoformat(),
        "actor": ev.actor,
        "delta_on_hand": round(delta_on_hand, 9),
        "delta_checked_out": round(delta_checked_out, 9),
        "affects_quantity": ev.event_type in _QTY_EVENTS,
        "detail": detail,
        "payload": p,
        "corrected": corrected,
        "correction_reason": correction_of.payload.get("reason")
        if correction_of
        else None,
        "corrected_by_event_uid": correction_of.event_uid if correction_of else None,
    }
    return entry


def quantity_evolution(session: Session, container_id: str) -> dict[str, Any]:
    """Full chain with running on-hand / checked-out balances.

    Conservation is asserted while walking: returned+consumed may never exceed
    what was checked out, and on-hand stock may never go negative.
    """
    status = container_status(session, container_id)
    raw = _load_events(session, container_id)
    effective = _effective_events(raw)

    correction_by_seq = {
        ev.payload["target_seq"]: ev
        for ev in raw
        if ev.event_type is EventType.CORRECTION
    }

    on_hand = 0.0
    checked_out = 0.0
    timeline: list[dict[str, Any]] = []
    conservation_violations: list[str] = []

    for ev in effective:
        entry = _timeline_entry(
            ev,
            corrected=ev.seq in correction_by_seq,
            correction_of=correction_by_seq.get(ev.seq),
        )
        on_hand += entry["delta_on_hand"]
        checked_out += entry["delta_checked_out"]
        if on_hand < -1e-9:
            conservation_violations.append(
                f"seq={ev.seq} 后在库量为负 ({on_hand:g})"
            )
        if checked_out < -1e-9:
            conservation_violations.append(
                f"seq={ev.seq} 后领用未还量为负 ({checked_out:g})"
            )
        entry["balance_on_hand"] = round(on_hand, 9)
        entry["balance_checked_out"] = round(checked_out, 9)
        timeline.append(entry)

    # corrections themselves are part of the visible trail
    for cev in raw:
        if cev.event_type is EventType.CORRECTION:
            timeline.append(
                {
                    "seq": cev.seq,
                    "event_uid": cev.event_uid,
                    "event_type": "CORRECTION",
                    "event_time": cev.event_time.isoformat(),
                    "actor": cev.actor,
                    "delta_on_hand": 0.0,
                    "delta_checked_out": 0.0,
                    "affects_quantity": False,
                    "detail": {
                        "target_seq": cev.payload["target_seq"],
                        "reason": cev.payload["reason"],
                        "original_event_type": cev.payload.get("original_event_type"),
                    },
                    "payload": cev.payload,
                    "corrected": False,
                    "correction_reason": None,
                    "corrected_by_event_uid": None,
                    "balance_on_hand": round(on_hand, 9),
                    "balance_checked_out": round(checked_out, 9),
                }
            )
    timeline.sort(key=lambda e: (e["seq"], 0 if e["event_type"] != "CORRECTION" else 1))

    return {
        "container_id": container_id,
        "final_on_hand_qty": round(on_hand, 9),
        "final_checked_out_qty": round(checked_out, 9),
        "projected_on_hand_qty": status["on_hand_qty"],
        "projected_checked_out_qty": status["checked_out_qty"],
        "balances_consistent": abs(on_hand - status["on_hand_qty"]) < 1e-6
        and abs(checked_out - status["checked_out_qty"]) < 1e-6,
        "conservation_violations": conservation_violations,
        "timeline": timeline,
    }


def destruction_references(
    session: Session, container_id: str
) -> dict[str, Any]:
    """Everything that blocks (or recently blocked) destruction."""
    refs = list(
        session.execute(
            select(InvestigationReference, Investigation)
            .join(
                Investigation,
                Investigation.investigation_id
                == InvestigationReference.investigation_id,
            )
            .where(InvestigationReference.container_id == container_id)
            .order_by(InvestigationReference.created_at)
        )
    )
    investigations = [
        {
            "investigation_id": inv.investigation_id,
            "title": inv.title,
            "status": inv.status,
            "active": bool(ref.active),
            "created_at": ref.created_at.isoformat(),
            "resolved_at": ref.resolved_at.isoformat() if ref.resolved_at else None,
        }
        for ref, inv in refs
    ]
    requests = list(
        session.scalars(
            select(DestructionRequest)
            .where(DestructionRequest.container_id == container_id)
            .order_by(DestructionRequest.requested_at)
        )
    )
    return {
        "container_id": container_id,
        "active_blockers": destruction_blockers(session, container_id=container_id),
        "investigation_references": investigations,
        "destruction_requests": [
            {
                "request_id": r.request_id,
                "status": r.status.value,
                "requested_by": r.requested_by,
                "requested_at": r.requested_at.isoformat(),
                "verifier_a": r.verifier_a,
                "verifier_b": r.verifier_b,
                "verified_at": r.verified_at.isoformat() if r.verified_at else None,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                "reject_reason": r.reject_reason,
            }
            for r in requests
        ],
    }


def pending_anomalies(session: Session) -> list[dict[str, Any]]:
    """Containers whose damaged labels await two-person identity verification."""
    rows = list(
        session.execute(
            select(Container, ContainerProjection)
            .join(
                ContainerProjection,
                ContainerProjection.container_id == Container.container_id,
            )
            .where(Container.state == ContainerState.ACTIVE)
            .order_by(Container.container_id)
        )
    )
    result = []
    for container, proj in rows:
        anomalies = []
        if proj.label_damaged:
            anomalies.append("标签受损，身份待双人核验")
        if proj.usage_restricted:
            anomalies.append(f"用途受限：{proj.usage_restricted}")
        if proj.investigation_hold:
            anomalies.append("调查保留中")
        if proj.legal_hold:
            anomalies.append("法律保留中")
        if proj.checked_out_qty > 1e-9:
            anomalies.append(
                f"领用未还 {proj.checked_out_qty:g}{proj.unit}（持有人 {proj.custodian}）"
            )
        if anomalies:
            result.append(
                {
                    "container_id": container.container_id,
                    "batch_id": container.batch_id,
                    "location": proj.location,
                    "label_damaged": bool(proj.label_damaged),
                    "pending_identity_verification": bool(
                        proj.pending_identity_verification
                    ),
                    "anomalies": anomalies,
                }
            )
    return result


def correction_trail(session: Session, container_id: str) -> list[dict[str, Any]]:
    """Every correction with both the original record and the corrected truth."""
    events = _load_events(session, container_id)
    by_seq = {ev.seq: ev for ev in events}
    trail = []
    for ev in events:
        if ev.event_type is not EventType.CORRECTION:
            continue
        target = by_seq.get(ev.payload["target_seq"])
        trail.append(
            {
                "correction_seq": ev.seq,
                "correction_event_uid": ev.event_uid,
                "corrected_at": ev.event_time.isoformat(),
                "corrected_by": ev.actor,
                "reason": ev.payload["reason"],
                "target_seq": ev.payload["target_seq"],
                "target_event_uid": ev.payload.get("target_event_uid"),
                "original_event_type": ev.payload.get("original_event_type"),
                "original_payload": ev.payload.get("original_payload"),
                "corrected_event_type": ev.payload.get("corrected_event_type"),
                "corrected_payload": ev.payload.get("corrected_payload"),
                "target_record_present": target is not None,
            }
        )
    return trail


def search(
    session: Session,
    *,
    container_id: str | None = None,
    batch_id: str | None = None,
    location: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(Container)
    if container_id:
        stmt = stmt.where(Container.container_id == container_id)
    if batch_id:
        stmt = stmt.where(Container.batch_id == batch_id)
    containers = list(session.scalars(stmt.order_by(Container.container_id)))

    result = []
    for c in containers:
        proj = session.get(ContainerProjection, c.container_id)
        if location is not None and (proj is None or proj.location != location):
            continue
        result.append(
            {
                "container_id": c.container_id,
                "batch_id": c.batch_id,
                "material": c.material,
                "state": c.state.value,
                "location": proj.location if proj else None,
                "custodian": proj.custodian if proj else None,
                "on_hand_qty": proj.quantity if proj else 0.0,
                "checked_out_qty": proj.checked_out_qty if proj else 0.0,
                "unit": proj.unit if proj else None,
                "usage_restricted": proj.usage_restricted if proj else None,
                "holds": [
                    name
                    for name, on in (
                        ("INVESTIGATION", proj.investigation_hold if proj else False),
                        ("LEGAL", proj.legal_hold if proj else False),
                    )
                    if on
                ],
            }
        )
    return result
