"""HTTP-level integration tests for the FastAPI app."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _receive(client, cid="C-1", **over):
    body = {
        "container_id": cid, "batch_id": "B1", "material": "原液",
        "quantity": 100.0, "unit": "mL", "location": "低温库A",
        "actor": "alice",
    }
    body.update(over)
    r = client.post("/samples/receive", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_lifecycle_over_http(client):
    _receive(client)
    r = client.post("/samples/aliquot", json={
        "parent_container_id": "C-1",
        "children": [{"container_id": "C-1A", "quantity": 30}],
        "storage_location": "架2", "actor": "alice",
    })
    assert r.status_code == 200, r.text

    r = client.post("/samples/checkout", json={
        "container_id": "C-1A", "holder": "bob", "quantity": 10,
        "purpose": "检验", "actor": "alice",
    })
    assert r.status_code == 200
    r = client.post("/samples/return", json={
        "container_id": "C-1A", "returned_qty": 8, "consumed_qty": 2,
        "to_location": "架2", "actor": "bob",
    })
    assert r.status_code == 200

    status = client.get("/samples/C-1A").json()
    assert status["on_hand_qty"] == 28.0
    assert status["custodian"] is None
    assert status["freeze_thaw_count"] == 1


def test_search_by_batch_and_location(client):
    _receive(client, cid="C-1", location="低温库A")
    _receive(client, cid="C-2", location="低温库B")
    _receive(client, cid="C-3", location="低温库A")

    r = client.get("/search", params={"batch_id": "B1"})
    assert r.json()["count"] == 3
    r = client.get("/search", params={"location": "低温库A"})
    assert {i["container_id"] for i in r.json()["items"]} == {"C-1", "C-3"}
    r = client.get("/search", params={"container_id": "C-2"})
    assert r.json()["items"][0]["location"] == "低温库B"


def test_timeline_endpoint_conservation(client):
    _receive(client)
    client.post("/samples/checkout", json={
        "container_id": "C-1", "holder": "bob", "quantity": 40,
        "purpose": "检验", "actor": "alice"})
    client.post("/samples/return", json={
        "container_id": "C-1", "returned_qty": 40, "consumed_qty": 0,
        "to_location": "低温库A", "actor": "bob"})
    data = client.get("/samples/C-1/timeline").json()
    assert data["balances_consistent"] is True
    assert data["final_on_hand_qty"] == 100.0
    assert [e["event_type"] for e in data["timeline"]] == [
        "RECEIVE", "CHECKOUT", "RETURN"]


def test_destruction_blocked_response_lists_blockers(client):
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _receive(client, retention_until=future)
    r = client.post("/destruction/request", json={
        "container_id": "C-1", "requested_by": "alice"})
    assert r.status_code == 409
    assert r.json()["error"] == "DestructionBlocked"
    assert any(b["code"] == "RETENTION_PERIOD" for b in r.json()["blockers"])


def test_destruction_workflow_over_http(client):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _receive(client, retention_until=past)
    r = client.post("/destruction/request", json={
        "container_id": "C-1", "requested_by": "alice"})
    request_id = r.json()["request_id"]

    r = client.post("/destruction/verify", json={
        "request_id": request_id, "verifier_a": "alice", "verifier_b": "bob",
        "scanned_code_a": "C-1", "scanned_code_b": "C-1"})
    assert r.status_code == 200, r.text
    r = client.post("/destruction/execute", json={
        "request_id": request_id, "actor": "alice"})
    assert r.status_code == 200
    assert client.get("/samples/C-1").json()["state"] == "DESTROYED"


def test_duplicate_idempotency_key_does_not_duplicate(client):
    key = "scan-xyz"
    _receive(client)
    payload = {"container_id": "C-1", "to_location": "低温库B",
               "actor": "alice", "request_key": key}
    r1 = client.post("/samples/move", json=payload)
    r2 = client.post("/samples/move", json=payload)
    assert r1.json()["seq"] == 2
    assert r2.json()["idempotent_replay"] is True
    timeline = client.get("/samples/C-1/timeline").json()
    assert len(timeline["timeline"]) == 2  # no phantom duplicate move


def test_validation_error_on_bad_quantity(client):
    r = client.post("/samples/receive", json={
        "container_id": "C-1", "batch_id": "B", "material": "m",
        "quantity": -5, "unit": "mL", "location": "L", "actor": "a"})
    assert r.status_code == 422


def test_404_for_unknown_container(client):
    assert client.get("/samples/NOPE").status_code == 404
    assert client.get("/samples/NOPE/timeline").status_code == 404


def test_correction_and_trail_over_http(client):
    _receive(client)
    client.post("/samples/move", json={
        "container_id": "C-1", "to_location": "错误位", "actor": "alice"})
    r = client.post("/samples/C-1/correct", json={
        "target_seq": 2, "actor": "sup", "reason": "扫错库位码",
        "corrected_event_type": "MOVE",
        "corrected_payload": {"from_location": "低温库A",
                              "to_location": "低温库C", "label_damaged": None}})
    assert r.status_code == 200, r.text
    trail = client.get("/samples/C-1/corrections").json()["corrections"]
    assert len(trail) == 1
    assert trail[0]["reason"] == "扫错库位码"
    assert client.get("/samples/C-1").json()["location"] == "低温库C"


def test_anomalies_endpoint(client):
    _receive(client, cid="C-OK")
    _receive(client, cid="C-BAD", label_damaged=True)
    client.post("/samples/move", json={
        "container_id": "C-BAD", "to_location": "暂存箱", "actor": "a",
        "label_damaged": True})
    items = client.get("/anomalies").json()["items"]
    bad = next(i for i in items if i["container_id"] == "C-BAD")
    assert bad["pending_identity_verification"] is True
    assert all(i["container_id"] != "C-OK" for i in items)


def test_two_person_identity_verification_clears_damaged_label(client):
    _receive(client, cid="C-BAD", label_damaged=True)
    client.post("/samples/move", json={
        "container_id": "C-BAD", "to_location": "暂存箱", "actor": "a",
        "label_damaged": True})

    # same person twice is rejected
    r = client.post("/samples/C-BAD/identity-verify", json={
        "container_id": "C-BAD", "verifier_a": "a", "verifier_b": "a",
        "scanned_code_a": "C-BAD", "scanned_code_b": "C-BAD"})
    assert r.status_code == 422

    # mismatched scan is rejected
    r = client.post("/samples/C-BAD/identity-verify", json={
        "container_id": "C-BAD", "verifier_a": "a", "verifier_b": "b",
        "scanned_code_a": "C-BAD", "scanned_code_b": "C-OTHER"})
    assert r.status_code == 422

    # two people, matching scans: anomaly cleared and vial relabelled
    r = client.post("/samples/C-BAD/identity-verify", json={
        "container_id": "C-BAD", "verifier_a": "mgr", "verifier_b": "qa",
        "scanned_code_a": "C-BAD", "scanned_code_b": "C-BAD",
        "note": "比对批次记录B1后确认身份"})
    assert r.status_code == 200, r.text

    status = client.get("/samples/C-BAD").json()
    assert status["label_damaged"] is False
    assert status["pending_identity_verification"] is False
    assert client.get("/anomalies").json()["count"] == 0
