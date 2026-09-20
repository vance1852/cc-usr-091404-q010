"""Destruction governance: retention, investigations, legal holds, dual control."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import queries, services
from app.errors import DestructionBlocked, LifecycleError
from app.models import ContainerState, RequestStatus

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=400)
PAST = NOW - timedelta(days=1)


def _receive(session, cid="C-1", retention_until=None, label_damaged=False):
    return services.receive_sample(
        session, container_id=cid, batch_id="B1", material="原液",
        quantity=10.0, unit="mL", location="低温库A", actor="alice",
        retention_until=retention_until, label_damaged=label_damaged,
        event_time=NOW,
    )


def test_cannot_request_destruction_before_retention_expires(session):
    _receive(session, retention_until=FUTURE)
    with pytest.raises(DestructionBlocked) as exc:
        services.request_destruction(session, container_id="C-1",
                                     requested_by="alice")
    codes = {b["code"] for b in exc.value.blockers}
    assert "RETENTION_PERIOD" in codes


def test_eligible_container_completes_full_workflow(session):
    _receive(session, retention_until=PAST)
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    assert req.status is RequestStatus.PENDING

    services.verify_destruction(
        session, request_id=req.request_id,
        verifier_a="alice", verifier_b="bob",
        scanned_code_a="C-1", scanned_code_b="C-1",
    )
    services.execute_destruction(session, request_id=req.request_id, actor="alice")

    from app.models import Container
    container = session.get(Container, "C-1")
    assert container.state is ContainerState.DESTROYED
    status = queries.container_status(session, "C-1")
    assert status["state"] == "DESTROYED"
    assert status["on_hand_qty"] == 0


def test_open_investigation_blocks_destruction(session):
    _receive(session, retention_until=PAST)
    services.open_investigation(
        session, investigation_id="INV-9", title="稳定性异常",
        container_ids=["C-1"], actor="qa",
    )
    with pytest.raises(DestructionBlocked) as exc:
        services.request_destruction(session, container_id="C-1",
                                     requested_by="alice")
    codes = {b["code"] for b in exc.value.blockers}
    assert "OPEN_INVESTIGATION" in codes

    # closing the investigation lifts the hold and frees destruction
    services.close_investigation(session, investigation_id="INV-9", actor="qa")
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    assert req.status is RequestStatus.PENDING


def test_legal_hold_blocks_even_after_retention(session):
    _receive(session, retention_until=PAST)
    services.set_legal_hold(session, container_id="C-1", active=True,
                            actor="legal", reason="诉讼保全", reference="CASE-1")
    with pytest.raises(DestructionBlocked) as exc:
        services.request_destruction(session, container_id="C-1",
                                     requested_by="alice")
    assert any(b["code"] == "LEGAL_HOLD" for b in exc.value.blockers)


def test_label_damaged_request_allowed_but_verification_requires_attestation(session):
    _receive(session, retention_until=PAST, label_damaged=True)
    # damaged label does not block the request itself — identity is settled
    # by the mandatory two-person verification before execution
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")

    with pytest.raises(LifecycleError, match="标签受损"):
        services.verify_destruction(
            session, request_id=req.request_id,
            verifier_a="alice", verifier_b="bob",
            scanned_code_a="C-1", scanned_code_b="C-1",
        )

    # after both staff compare batch records and attest the physical identity
    services.verify_destruction(
        session, request_id=req.request_id,
        verifier_a="alice", verifier_b="bob",
        scanned_code_a="C-1", scanned_code_b="C-1",
        identity_attested=True,
    )
    services.execute_destruction(session, request_id=req.request_id, actor="alice")
    assert queries.container_status(session, "C-1")["state"] == "DESTROYED"


def test_verifiers_must_be_two_different_people(session):
    _receive(session, retention_until=PAST)
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    with pytest.raises(LifecycleError, match="两名不同人员"):
        services.verify_destruction(
            session, request_id=req.request_id,
            verifier_a="alice", verifier_b="alice",
            scanned_code_a="C-1", scanned_code_b="C-1",
        )


def test_scanned_code_must_match_container(session):
    _receive(session, retention_until=PAST)
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    # second verifier scans a neighbouring vial — execution must be blocked
    with pytest.raises(LifecycleError, match="容器身份核验失败"):
        services.verify_destruction(
            session, request_id=req.request_id,
            verifier_a="alice", verifier_b="bob",
            scanned_code_a="C-1", scanned_code_b="C-2",
        )
    req = session.get(type(req), req.request_id)
    assert req.status is RequestStatus.PENDING


def test_cannot_execute_without_dual_verification(session):
    _receive(session, retention_until=PAST)
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    with pytest.raises(LifecycleError, match="双人核验"):
        services.execute_destruction(session, request_id=req.request_id,
                                     actor="alice")


def test_hold_placed_between_request_and_execution_blocks_execution(session):
    _receive(session, retention_until=PAST)
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    services.verify_destruction(
        session, request_id=req.request_id,
        verifier_a="alice", verifier_b="bob",
        scanned_code_a="C-1", scanned_code_b="C-1",
    )
    # stability investigation opens at the last minute — re-check must catch it
    services.open_investigation(
        session, investigation_id="INV-LATE", title="新增异常",
        container_ids=["C-1"], actor="qa",
    )
    with pytest.raises(DestructionBlocked) as exc:
        services.execute_destruction(session, request_id=req.request_id,
                                     actor="alice")
    assert any(b["code"] == "OPEN_INVESTIGATION" for b in exc.value.blockers)


def test_checked_out_container_blocks_destruction(session):
    _receive(session, retention_until=PAST)
    services.checkout(session, container_id="C-1", holder="bob", quantity=3,
                      purpose="INVESTIGATION", actor="alice")
    with pytest.raises(DestructionBlocked) as exc:
        services.request_destruction(session, container_id="C-1",
                                     requested_by="alice")
    assert any(b["code"] == "CHECKED_OUT" for b in exc.value.blockers)


def test_destroyed_container_rejects_further_events(session):
    _receive(session, retention_until=PAST)
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    services.verify_destruction(
        session, request_id=req.request_id, verifier_a="alice", verifier_b="bob",
        scanned_code_a="C-1", scanned_code_b="C-1")
    services.execute_destruction(session, request_id=req.request_id, actor="alice")
    with pytest.raises(LifecycleError, match="已销毁"):
        services.move(session, container_id="C-1", to_location="X", actor="a")


def test_destroyed_container_keeps_full_history(session):
    _receive(session, retention_until=PAST)
    services.move(session, container_id="C-1", to_location="低温库B", actor="alice")
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    services.verify_destruction(
        session, request_id=req.request_id, verifier_a="alice", verifier_b="bob",
        scanned_code_a="C-1", scanned_code_b="C-1")
    services.execute_destruction(session, request_id=req.request_id, actor="alice")

    evolution = queries.quantity_evolution(session, "C-1")
    types = [e["event_type"] for e in evolution["timeline"]]
    assert types == ["RECEIVE", "MOVE", "DESTRUCTION_REQUEST",
                     "DESTRUCTION_VERIFY", "DESTROY"]
    assert queries.correction_trail(session, "C-1") == []
