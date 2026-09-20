"""管理员查询视图：按容器/批次/库位/人员检索，待核验异常总览，链完整性。"""

from conftest import (aliquot, checkout, do_return, get_container, move,
                      receive, temperature)


def _build_scene(c):
    """两个容器：一个正常流转，一个标签受损入暂存箱。"""
    receive(c, code="CNT-1", event_id="evt-recv-1", quantity=200.0)
    aliquot(c, parent="CNT-1", event_id="evt-ali-1",
            children=[{"container_code": "CNT-1-A", "quantity": 50.0}])
    checkout(c, code="CNT-1-A", person="TECH-1", event_id="evt-co-a")
    do_return(c, code="CNT-1-A", qty=45.0, event_id="evt-ret-a")

    receive(c, code="CNT-2", event_id="evt-recv-2", quantity=80.0)
    c.post("/events/flag-anomaly", json={
        "event_id": "evt-flag-2", "container_code": "CNT-2",
        "anomaly_type": "LABEL_DAMAGED", "note": "标签磨损", "actor_code": "ADMIN"})
    move(c, code="CNT-2", to="STAGING-1", event_id="evt-move-2")


def test_query_by_container_batch_location_person(seeded):
    c = seeded
    _build_scene(c)

    # 按容器：实际持有人与数量
    c1 = get_container(c, "CNT-1")
    assert c1["current_quantity"] == 150.0
    assert c1["holder"]["code"] == "FREEZER-A"
    ca = get_container(c, "CNT-1-A")
    assert ca["current_quantity"] == 45.0
    assert ca["holder"]["code"] == "FREEZER-A"

    # 按批次
    batch_view = c.get("/batches/LOT-1/containers").json()
    assert sorted(x["code"] for x in batch_view) == ["CNT-1", "CNT-1-A", "CNT-2"]

    # 按库位
    staging = c.get("/locations/STAGING-1/containers").json()
    assert [x["code"] for x in staging] == ["CNT-2"]
    assert staging[0]["status"] == "PENDING_VERIFICATION"
    assert staging[0]["label_damaged"] is True

    # 列表过滤
    by_status = c.get("/containers?status=PENDING_VERIFICATION").json()
    assert [x["code"] for x in by_status] == ["CNT-2"]
    by_location = c.get("/containers?location_code=STAGING-1").json()
    assert [x["code"] for x in by_location] == ["CNT-2"]


def test_quantity_history_reflects_full_evolution(seeded):
    c = seeded
    _build_scene(c)
    hist = c.get("/containers/CNT-1-A/quantity-history").json()
    pairs = [(h["event_type"], h["quantity_before"], h["quantity_after"]) for h in hist]
    assert pairs[0] == ("ALIQUOT_CHILD", 0.0, 50.0)
    assert ("CHECKOUT", 50.0, 50.0) in pairs
    assert ("RETURN", 50.0, 45.0) in pairs
    # 母容器：分装扣减可见
    parent_hist = c.get("/containers/CNT-1/quantity-history").json()
    ali = next(h for h in parent_hist if h["event_type"] == "ALIQUOT")
    assert ali["quantity_before"] == 200.0
    assert ali["quantity_after"] == 150.0
    assert ali["delta"] == -50.0


def test_pending_anomalies_overview(seeded):
    c = seeded
    _build_scene(c)
    # 再制造一个超限容器与一个待核验销毁申请
    receive(c, code="CNT-3", event_id="evt-recv-3", quantity=10.0,
            retention_until="2025-01-01")
    for i in range(3):
        temperature(c, code="CNT-3", event_id=f"t3-{i}")
    c.post("/destruction-requests", json={
        "request_id": "DR-3", "container_code": "CNT-3",
        "reason": "超限处置", "requested_by": "ADMIN"})

    overview = c.get("/anomalies/pending").json()
    assert [x["code"] for x in overview["containers_pending_verification"]] == ["CNT-2"]
    assert [x["code"] for x in overview["restricted_containers"]] == ["CNT-3"]
    awaiting = overview["destruction_requests_awaiting_verification"]
    assert [r["request_id"] for r in awaiting] == ["DR-3"]
    assert overview["destruction_requests_blocked"] == []


def test_blocked_destruction_references_listed(seeded):
    c = seeded
    receive(c, code="CNT-7", event_id="evt-recv-7", retention_until="2025-06-01")
    c.post("/investigations", json={"code": "INV-7", "title": "稳定性调查"})
    c.post("/investigations/INV-7/containers", json={"container_code": "CNT-7"})
    c.post("/containers/CNT-7/legal-holds", json={
        "reason": "诉讼保全", "person_code": "QA-1"})
    c.post("/destruction-requests", json={
        "request_id": "DR-7", "container_code": "CNT-7",
        "reason": "清理", "requested_by": "ADMIN"})

    view = c.get("/containers/CNT-7/destruction-blockers").json()
    assert view["blocked"] is True
    types = sorted(b["type"] for b in view["blockers"])
    assert types == ["LEGAL_HOLD", "OPEN_INVESTIGATION"]
    assert view["active_request"]["request_id"] == "DR-7"
    assert view["active_request"]["status"] == "BLOCKED"


def test_hash_chain_detects_tampering(seeded):
    c = seeded
    _build_scene(c)
    assert c.get("/events/verify-chain").json()["valid"] is True

    # 模拟直接改库篡改事件载荷
    from sqlalchemy import select
    from app.db import get_db
    from app.main import app
    from app.models import Event
    override = app.dependency_overrides[get_db]
    gen = override()
    db = next(gen)
    ev = db.scalars(select(Event).where(Event.event_id == "evt-recv-1")).first()
    ev.payload = {**ev.payload, "quantity": 99999}
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass

    result = c.get("/events/verify-chain").json()
    assert result["valid"] is False
    assert result["first_bad_seq"] == ev.seq
