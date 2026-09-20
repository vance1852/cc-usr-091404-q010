"""End-to-end scenario from the annual cleanup brief.

Two vials surface during the yearly cold-store cleanup:
* C-STOCK-1 — a bulk retention sample pending destruction, but still
  referenced by an open stability investigation;
* C-STOCK-2 — a vial whose label was damaged and which has already been
  moved into the staging box.

Destroying straight from the checklist would either break investigation
evidence or mishandle a valid retention sample.  The system must block both
and give the administrator the full picture.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import queries, services
from app.errors import DestructionBlocked

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _setup(session):
    # Vial 1: past regulatory retention, bulk stock scheduled for destruction,
    # but the stability investigation still pulls it.
    services.receive_sample(
        session, container_id="C-STOCK-1", batch_id="B2025-A",
        material="原液-单抗X", quantity=10.0, unit="mL",
        location="低温库A-架1", actor="mgr",
        retention_until=NOW - timedelta(days=30),
        event_time=NOW - timedelta(days=400),
    )
    services.open_investigation(
        session, investigation_id="STAB-2026-07",
        title="年度稳定性趋势调查", container_ids=["C-STOCK-1"], actor="qa",
    )

    # Vial 2: valid retention sample; label torn during handling, moved to
    # the staging box pending identity verification.
    services.receive_sample(
        session, container_id="C-STOCK-2", batch_id="B2025-A",
        material="原液-单抗X", quantity=10.0, unit="mL",
        location="低温库A-架1", actor="mgr",
        retention_until=NOW + timedelta(days=300),
        event_time=NOW - timedelta(days=200),
    )
    services.move(session, container_id="C-STOCK-2",
                  to_location="暂存箱", actor="mgr", label_damaged=True)


def test_checklist_destruction_is_blocked_for_both_vials(session):
    _setup(session)

    # vial 1: retention expired, but the open investigation blocks destruction
    try:
        services.request_destruction(session, container_id="C-STOCK-1",
                                     requested_by="mgr")
        assert False, "应当被未结调查阻断"
    except DestructionBlocked as exc:
        codes = {b["code"] for b in exc.blockers}
        assert codes == {"OPEN_INVESTIGATION"}

    # vial 2: still inside its regulatory retention period
    try:
        services.request_destruction(session, container_id="C-STOCK-2",
                                     requested_by="mgr")
        assert False, "应当被保存期阻断"
    except DestructionBlocked as exc:
        codes = {b["code"] for b in exc.blockers}
        assert codes == {"RETENTION_PERIOD"}


def test_admin_queries_show_holder_evolution_blockers_and_anomalies(session):
    _setup(session)

    s1 = queries.container_status(session, "C-STOCK-1")
    assert s1["custodian"] is None
    assert s1["location"] == "低温库A-架1"
    assert s1["investigation_hold"] is True

    refs = queries.destruction_references(session, "C-STOCK-1")
    assert refs["active_blockers"][0]["code"] == "OPEN_INVESTIGATION"
    assert refs["investigation_references"][0]["investigation_id"] == "STAB-2026-07"
    assert refs["investigation_references"][0]["status"] == "OPEN"

    # damaged-label vial appears on the pending-verification anomaly list
    anomalies = queries.pending_anomalies(session)
    by_id = {a["container_id"]: a for a in anomalies}
    assert "C-STOCK-2" in by_id
    assert by_id["C-STOCK-2"]["pending_identity_verification"] is True
    assert any("标签受损" in x for x in by_id["C-STOCK-2"]["anomalies"])
    # vial under investigation hold also surfaces
    assert "C-STOCK-1" in by_id

    evo1 = queries.quantity_evolution(session, "C-STOCK-1")
    assert evo1["balances_consistent"] is True


def test_proper_resolution_after_investigation_closes(session):
    _setup(session)
    # investigation concludes and releases the reference
    services.close_investigation(session, investigation_id="STAB-2026-07",
                                 actor="qa")
    refs = queries.destruction_references(session, "C-STOCK-1")
    assert refs["investigation_references"][0]["active"] is False
    assert refs["active_blockers"] == []

    # now the full governed workflow may run with dual identity verification
    req = services.request_destruction(session, container_id="C-STOCK-1",
                                       requested_by="mgr")
    services.verify_destruction(
        session, request_id=req.request_id,
        verifier_a="mgr", verifier_b="qa",
        scanned_code_a="C-STOCK-1", scanned_code_b="C-STOCK-1",
    )
    services.execute_destruction(session, request_id=req.request_id, actor="mgr")
    assert queries.container_status(session, "C-STOCK-1")["state"] == "DESTROYED"

    # vial 2 remains untouched and valid, visible in the staging box
    s2 = queries.container_status(session, "C-STOCK-2")
    assert s2["state"] == "ACTIVE"
    assert s2["location"] == "暂存箱"
    assert s2["on_hand_qty"] == 10.0


def test_wrong_scan_on_damaged_vial_blocks_execution(session):
    _setup(session)
    # retention actually expired (registration corrected), so the request is
    # accepted — but the damaged label must be cleared by two-person checks
    from app.models import Event
    ev1 = session.query(Event).filter_by(container_id="C-STOCK-2", seq=1).one()
    payload = dict(ev1.payload)
    payload["retention_until"] = (NOW - timedelta(days=1)).isoformat()
    services.correct_event(
        session, container_id="C-STOCK-2", target_seq=1, actor="supervisor",
        reason="保存期实际已满，更正登记日期", corrected_event_type="RECEIVE",
        corrected_payload=payload,
    )
    req = services.request_destruction(session, container_id="C-STOCK-2",
                                       requested_by="mgr")

    # a wrong scan on the damaged vial is rejected even with attestation
    import pytest
    from app.errors import LifecycleError
    with pytest.raises(LifecycleError, match="容器身份核验失败"):
        services.verify_destruction(
            session, request_id=req.request_id,
            verifier_a="mgr", verifier_b="qa",
            scanned_code_a="C-STOCK-2", scanned_code_b="C-STOCK-1",
            identity_attested=True,
        )
    # correct scans from two people, identity attested against batch records
    services.verify_destruction(
        session, request_id=req.request_id,
        verifier_a="mgr", verifier_b="qa",
        scanned_code_a="C-STOCK-2", scanned_code_b="C-STOCK-2",
        identity_attested=True,
    )
    services.execute_destruction(session, request_id=req.request_id, actor="mgr")
    assert queries.container_status(session, "C-STOCK-2")["state"] == "DESTROYED"
