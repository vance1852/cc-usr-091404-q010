"""Hash chain integrity and idempotent scan resistance."""
from __future__ import annotations

from app import services
from app.ledger import verify_container_chain
from app.models import Event


def _receive(session, cid="C-1", **kw):
    params = dict(
        container_id=cid,
        batch_id="B1",
        material="原液",
        quantity=100.0,
        unit="mL",
        location="低温库A",
        actor="alice",
    )
    params.update(kw)
    return services.receive_sample(session, **params)


def test_chain_hashes_link_and_verify(session):
    e1 = _receive(session)
    e2 = services.move(session, container_id="C-1", to_location="低温库B", actor="alice")
    e3 = services.record_temperature_exposure(
        session, container_id="C-1", duration_min=10, actor="alice"
    )

    assert e1.prev_hash is None
    assert e2.prev_hash == e1.event_hash
    assert e3.prev_hash == e2.event_hash
    assert verify_container_chain(session, "C-1") == []


def test_aliquot_child_chain_anchored_to_parent_event(session):
    _receive(session, cid="C-MOM", quantity=100)
    events = services.aliquot(
        session,
        parent_container_id="C-MOM",
        children=[{"container_id": "C-KID", "quantity": 30}],
        storage_location="架2",
        actor="alice",
    )
    parent_event, child_event = events
    assert child_event.prev_hash == parent_event.event_hash
    assert verify_container_chain(session, "C-MOM") == []
    assert verify_container_chain(session, "C-KID") == []


def test_tampering_with_payload_is_detected(session):
    _receive(session)
    # an attacker (or a buggy direct DB edit) rewrites history
    ev = session.query(Event).filter_by(seq=1).one()
    ev.payload = {**ev.payload, "quantity": 999.0}
    session.flush()

    problems = verify_container_chain(session, "C-1")
    assert len(problems) == 1
    assert "content hash mismatch" in problems[0]


def test_tampering_with_prev_link_is_detected(session):
    _receive(session)
    services.move(session, container_id="C-1", to_location="低温库B", actor="alice")
    ev2 = session.query(Event).filter_by(seq=2).one()
    ev2.prev_hash = "0" * 64
    session.flush()

    problems = verify_container_chain(session, "C-1")
    assert any("prev_hash mismatch" in p for p in problems)


def test_reordered_event_breaks_chain(session):
    _receive(session)
    services.move(session, container_id="C-1", to_location="低温库B", actor="alice")
    e1 = session.query(Event).filter_by(seq=1).one()
    e2 = session.query(Event).filter_by(seq=2).one()
    # swap sequence numbers via a temporary value to dodge the unique index
    e1.seq = 99
    session.flush()
    e2.seq = 1
    session.flush()
    e1.seq = 2
    session.flush()

    problems = verify_container_chain(session, "C-1")
    assert problems  # hashes no longer line up


def test_missing_event_breaks_chain(session):
    _receive(session)
    services.move(session, container_id="C-1", to_location="低温库B", actor="alice")
    session.query(Event).filter_by(seq=1).delete()
    session.flush()
    # the remaining event's prev_hash now points to nothing valid
    head = session.query(Event).filter_by(seq=2).one()
    head.seq = 1
    session.flush()
    assert verify_container_chain(session, "C-1")


def test_duplicate_scan_raises_replay_carrying_original_event(session):
    import pytest
    from app.ledger import IdempotentReplay

    e1 = _receive(session, request_key="scan-777")
    with pytest.raises(IdempotentReplay) as exc:
        _receive(session, request_key="scan-777")  # same barcode rescan
    assert exc.value.event.id == e1.id
    assert session.query(Event).filter(Event.container_id == "C-1").count() == 1


def test_duplicate_scan_via_api_returns_200_and_marks_replay(client):
    body = {
        "container_id": "C-API-1", "batch_id": "B", "material": "原液",
        "quantity": 10, "unit": "mL", "location": "库A", "actor": "a",
        "request_key": "k-1",
    }
    r1 = client.post("/samples/receive", json=body)
    r2 = client.post("/samples/receive", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["event"]["event_uid"] == r1.json()["event_uid"]


def test_chain_verify_endpoint_reports_clean(client):
    client.post("/samples/receive", json={
        "container_id": "C-X", "batch_id": "B", "material": "m",
        "quantity": 5, "unit": "mL", "location": "L", "actor": "a"})
    r = client.get("/chain/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["problems"] == {}
