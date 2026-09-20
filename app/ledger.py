"""Append-only hash-chained ledger.

Every event is content-addressed inside its container's chain::

    event_hash = sha256(prev_hash or "" || canonical(event fields))

``prev_hash`` is the hash of that container's previous event (for the first
event of a child aliquot it is the parent ALIQUOT event hash, via
``Container.anchor_event_hash``).  Nothing is ever updated or deleted:
mistakes are undone by appending a CORRECTION event, and projections are
recovered by replaying the ledger.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Container, Event, EventType


def new_event_uid() -> str:
    return "ev_" + uuid.uuid4().hex


def canonical_bytes(
    *,
    container_id: str,
    seq: int,
    event_type: str,
    event_time: datetime,
    actor: str,
    payload: dict[str, Any],
    prev_hash: str | None,
) -> bytes:
    # SQLite drops tzinfo on round-trip; normalise explicitly to UTC so hash
    # verification is independent of the server's local timezone.
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    else:
        event_time = event_time.astimezone(timezone.utc)
    body = {
        "container_id": container_id,
        "seq": seq,
        "event_type": event_type,
        "event_time": event_time.isoformat(),
        "actor": actor,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_hash(**kwargs) -> str:
    return hashlib.sha256(canonical_bytes(**kwargs)).hexdigest()


class IdempotentReplay(Exception):
    """A command was retried with a request_key already seen.

    Carries the original event so the API can return it unchanged.
    """

    def __init__(self, event: Event):
        self.event = event
        super().__init__(f"idempotent replay of event {event.event_uid}")


def append_event(
    session: Session,
    *,
    container_id: str,
    event_type: EventType,
    actor: str,
    payload: dict[str, Any],
    event_time: datetime,
    idempotency_key: str | None = None,
) -> Event:
    """Append an event to a container's chain.

    Raises ``IdempotentReplay`` (no mutation) when ``idempotency_key`` was
    already consumed — this is what makes a repeated barcode scan harmless.
    """
    if idempotency_key is not None:
        existing = session.scalar(
            select(Event).where(Event.idempotency_key == idempotency_key)
        )
        if existing is not None:
            raise IdempotentReplay(existing)

    container = session.get(Container, container_id)
    if container is None:
        raise ValueError(f"unknown container {container_id!r}")

    seq = container.last_seq + 1
    prev_hash = container.last_event_hash or container.anchor_event_hash
    event_hash = compute_hash(
        container_id=container_id,
        seq=seq,
        event_type=event_type.value,
        event_time=event_time,
        actor=actor,
        payload=payload,
        prev_hash=prev_hash,
    )

    event = Event(
        event_uid=new_event_uid(),
        container_id=container_id,
        seq=seq,
        event_type=event_type,
        event_time=event_time,
        actor=actor,
        payload=payload,
        prev_hash=prev_hash,
        event_hash=event_hash,
        idempotency_key=idempotency_key,
    )
    session.add(event)
    session.flush()  # assign event.id

    container.last_seq = seq
    container.last_event_hash = event_hash
    session.flush()
    return event


def verify_container_chain(session: Session, container_id: str) -> list[str]:
    """Recompute every hash of one chain. Returns a list of problems (empty=ok)."""
    container = session.get(Container, container_id)
    if container is None:
        return [f"unknown container {container_id}"]

    problems: list[str] = []
    events = list(
        session.scalars(
            select(Event)
            .where(Event.container_id == container_id)
            .order_by(Event.seq)
        )
    )
    expected_prev = container.anchor_event_hash
    for event in events:
        want = compute_hash(
            container_id=event.container_id,
            seq=event.seq,
            event_type=event.event_type.value,
            event_time=event.event_time,
            actor=event.actor,
            payload=event.payload,
            prev_hash=expected_prev,
        )
        if event.prev_hash != expected_prev:
            problems.append(
                f"{container_id} seq={event.seq}: prev_hash mismatch "
                f"(stored {event.prev_hash}, expected {expected_prev})"
            )
        if event.event_hash != want:
            problems.append(
                f"{container_id} seq={event.seq}: content hash mismatch (tampered?)"
            )
        expected_prev = event.event_hash

    if container.last_event_hash != expected_prev:
        problems.append(f"{container_id}: chain head does not match container record")
    return problems


def verify_all_chains(session: Session) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for cid in session.scalars(select(Container.container_id)):
        problems = verify_container_chain(session, cid)
        if problems:
            result[cid] = problems
    return result
