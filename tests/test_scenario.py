"""端到端场景：年度清理低温库 —— 调查引用阻断销毁 + 标签受损样品防错处置。"""

from conftest import get_container, receive


def test_annual_cleanup_scenario(seeded):
    c = seeded

    # 1. 一支原液留样（保存期已过，列入待销毁清单）
    receive(c, code="STOCK-001", event_id="evt-recv-stock", quantity=500.0,
            retention_until="2025-12-31")
    # 2. 该留样被稳定性调查引用
    c.post("/investigations", json={"code": "INV-STAB-2026", "title": "稳定性调查"})
    c.post("/investigations/INV-STAB-2026/containers",
           json={"container_code": "STOCK-001"})
    # 3. 另一支标签受损样品已移入暂存箱
    receive(c, code="LBL-002", event_id="evt-recv-lbl", quantity=50.0,
            retention_until="2025-12-31")
    c.post("/events/flag-anomaly", json={
        "event_id": "evt-flag-lbl", "container_code": "LBL-002",
        "anomaly_type": "LABEL_DAMAGED", "note": "冷链标签受潮", "actor_code": "ADMIN"})
    c.post("/events/move", json={
        "event_id": "evt-move-lbl", "container_code": "LBL-002",
        "to_location_code": "STAGING-1", "actor_code": "ADMIN"})

    # 4. 管理员按清单申请销毁两支 —— 双双被阻断，证据链与有效留样都安全
    r1 = c.post("/destruction-requests", json={
        "request_id": "DR-STOCK", "container_code": "STOCK-001",
        "reason": "年度清理", "requested_by": "ADMIN"})
    assert r1.json()["status"] == "BLOCKED"
    assert [b["type"] for b in r1.json()["blockers"]] == ["OPEN_INVESTIGATION"]

    r2 = c.post("/destruction-requests", json={
        "request_id": "DR-LBL", "container_code": "LBL-002",
        "reason": "年度清理", "requested_by": "ADMIN"})
    assert r2.json()["status"] == "BLOCKED"
    assert [b["type"] for b in r2.json()["blockers"]] == ["ANOMALY_OPEN"]

    # 5. 待核验异常总览一眼看全
    overview = c.get("/anomalies/pending").json()
    assert [x["code"] for x in overview["containers_pending_verification"]] == ["LBL-002"]
    blocked = {r["request_id"] for r in overview["destruction_requests_blocked"]}
    assert blocked == {"DR-STOCK", "DR-LBL"}

    # 6. 标签样品身份经双人复扫码确认（更正异常状态），调查结案后，方可销毁
    c.post("/events/resolve-anomaly", json={
        "event_id": "evt-resolve-lbl", "container_code": "LBL-002",
        "confirmed_container_code": "LBL-002", "actor_code": "QA-1"})
    assert c.post("/destruction-requests/DR-LBL/recheck").json()["status"] == "PENDING"
    c.post("/destruction-requests/DR-LBL/verify",
           json={"person_code": "QA-1", "scanned_container_code": "LBL-002"})
    c.post("/destruction-requests/DR-LBL/verify",
           json={"person_code": "QA-2", "scanned_container_code": "LBL-002"})
    r = c.post("/destruction-requests/DR-LBL/execute",
               json={"person_code": "QA-2", "event_id": "evt-destroy-lbl"})
    assert r.status_code == 201
    assert get_container(c, "LBL-002")["status"] == "DESTROYED"

    # 7. 被调查引用的留样在调查结案前依旧无法销毁
    r = c.post("/destruction-requests/DR-STOCK/verify",
               json={"person_code": "QA-1", "scanned_container_code": "STOCK-001"})
    assert r.status_code == 409  # 仍处于 BLOCKED
    c.post("/investigations/INV-STAB-2026/close")
    assert c.post("/destruction-requests/DR-STOCK/recheck").json()["status"] == "PENDING"

    # 8. 全库事件链完整可验
    assert c.get("/events/verify-chain").json()["valid"] is True
