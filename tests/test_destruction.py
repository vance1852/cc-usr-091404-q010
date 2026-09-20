"""销毁工作流：法规保存期 / 未结调查 / 法律保留阻断，双人核验容器身份后执行。"""

from conftest import get_container, receive


def _request(c, container="CNT-1", req="DR-1", by="ADMIN"):
    return c.post("/destruction-requests", json={
        "request_id": req, "container_code": container,
        "reason": "年度清理", "requested_by": by})


def _verify(c, req, person, scanned):
    return c.post(f"/destruction-requests/{req}/verify", json={
        "person_code": person, "scanned_container_code": scanned})


def _execute(c, req, person, event_id="evt-destroy-1"):
    return c.post(f"/destruction-requests/{req}/execute", json={
        "person_code": person, "event_id": event_id})


def _past_retention(c, code="CNT-1"):
    receive(c, code=code, retention_until="2025-01-01")  # 保存期已过


def test_open_investigation_blocks_destruction(seeded):
    """场景还原：待销毁的原液留样仍被稳定性调查引用。"""
    c = seeded
    _past_retention(c)
    c.post("/investigations", json={"code": "INV-STAB", "title": "稳定性调查"})
    c.post("/investigations/INV-STAB/containers", json={"container_code": "CNT-1"})

    r = _request(c)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "BLOCKED"
    types = [b["type"] for b in body["blockers"]]
    assert "OPEN_INVESTIGATION" in types
    assert body["blockers"][0]["investigation_code"] == "INV-STAB"

    # 阻断的引用可通过容器视角查询
    view = c.get("/containers/CNT-1/destruction-blockers").json()
    assert view["blocked"] is True
    assert view["blockers"][0]["type"] == "OPEN_INVESTIGATION"

    # 调查结案后复检放行
    c.post("/investigations/INV-STAB/close")
    r = c.post("/destruction-requests/DR-1/recheck")
    assert r.json()["status"] == "PENDING"
    assert r.json()["blockers"] == []


def test_retention_period_blocks_destruction(seeded):
    c = seeded
    receive(c, retention_until="2099-01-01")  # 保存期远未到期
    r = _request(c)
    assert r.json()["status"] == "BLOCKED"
    assert r.json()["blockers"][0]["type"] == "RETENTION_PERIOD"


def test_legal_hold_blocks_and_release_unblocks(seeded):
    c = seeded
    _past_retention(c)
    hold = c.post("/containers/CNT-1/legal-holds", json={
        "reason": "监管检查", "person_code": "QA-1"})
    assert hold.status_code == 201
    r = _request(c)
    assert r.json()["status"] == "BLOCKED"
    assert r.json()["blockers"][0]["type"] == "LEGAL_HOLD"

    hold_id = hold.json()["id"]
    c.post(f"/legal-holds/{hold_id}/release", json={"person_code": "QA-1"})
    r = c.post("/destruction-requests/DR-1/recheck")
    assert r.json()["status"] == "PENDING"


def test_pending_verification_container_cannot_be_destroyed(seeded):
    """场景还原：标签受损样品在暂存箱，身份未确认前禁止销毁。"""
    c = seeded
    _past_retention(c)
    c.post("/events/flag-anomaly", json={
        "event_id": "evt-flag-1", "container_code": "CNT-1",
        "anomaly_type": "LABEL_DAMAGED", "note": "标签磨损", "actor_code": "ADMIN"})
    c.post("/events/move", json={
        "event_id": "evt-move-staging", "container_code": "CNT-1",
        "to_location_code": "STAGING-1", "actor_code": "ADMIN"})

    r = _request(c)
    assert r.json()["status"] == "BLOCKED"
    assert r.json()["blockers"][0]["type"] == "ANOMALY_OPEN"

    # 重新扫码确认身份后解除异常，复检通过
    bad = c.post("/events/resolve-anomaly", json={
        "event_id": "evt-resolve-1", "container_code": "CNT-1",
        "confirmed_container_code": "CNT-WRONG", "actor_code": "QA-1"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "CONTAINER_MISMATCH"

    ok = c.post("/events/resolve-anomaly", json={
        "event_id": "evt-resolve-2", "container_code": "CNT-1",
        "confirmed_container_code": "CNT-1", "actor_code": "QA-1"})
    assert ok.status_code == 201
    assert get_container(c)["status"] == "ACTIVE"
    assert c.post("/destruction-requests/DR-1/recheck").json()["status"] == "PENDING"


def test_dual_verification_requires_two_distinct_people_and_matching_scan(seeded):
    c = seeded
    _past_retention(c)
    _request(c)

    # 条码不匹配：核验失败（防止错处置有效留样）
    r = _verify(c, "DR-1", "QA-1", "CNT-WRONG")
    assert r.status_code == 400
    assert r.json()["code"] == "CONTAINER_MISMATCH"

    # 第一人核验
    r = _verify(c, "DR-1", "QA-1", "CNT-1")
    assert r.json()["status"] == "PENDING"
    assert r.json()["verify1_by"] == "QA-1"
    assert r.json()["verify2_by"] is None

    # 同一人重复扫码：幂等，不推进
    r = _verify(c, "DR-1", "QA-1", "CNT-1")
    assert r.json()["status"] == "PENDING"
    assert r.json()["verify2_by"] is None

    # 第二人核验完成
    r = _verify(c, "DR-1", "QA-2", "CNT-1")
    assert r.json()["status"] == "VERIFIED"
    assert r.json()["verify2_by"] == "QA-2"


def test_execute_requires_verification_and_destroys_container(seeded):
    c = seeded
    _past_retention(c)
    _request(c)
    # 未核验直接执行
    r = _execute(c, "DR-1", "QA-1")
    assert r.status_code == 409
    assert r.json()["code"] == "NOT_VERIFIED"

    _verify(c, "DR-1", "QA-1", "CNT-1")
    _verify(c, "DR-1", "QA-2", "CNT-1")

    # 非核验人不能执行
    r = _execute(c, "DR-1", "ADMIN")
    assert r.status_code == 400
    assert r.json()["code"] == "EXECUTOR_NOT_VERIFIER"

    r = _execute(c, "DR-1", "QA-1")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "EXECUTED"

    ctn = get_container(c)
    assert ctn["status"] == "DESTROYED"
    assert ctn["current_quantity"] == 0.0
    assert ctn["holder"]["type"] == "NONE"
    # 销毁事件在链上
    types = [e["event_type"] for e in c.get("/containers/CNT-1/events").json()]
    assert types[-1] == "DESTROY"
    # 重复执行幂等
    assert _execute(c, "DR-1", "QA-1").status_code == 200
    # 已销毁容器不能再申请
    r = _request(c, req="DR-2")
    assert r.status_code == 400


def test_destruction_request_idempotent_and_unique_per_container(seeded):
    c = seeded
    _past_retention(c)
    r1 = _request(c)
    r2 = _request(c)  # 同 request_id 幂等
    assert r1.status_code == 201 and r2.status_code == 200
    r3 = _request(c, req="DR-OTHER")  # 同容器新申请冲突
    assert r3.status_code == 409


def test_recheck_at_execute_catches_new_blocker(seeded):
    """核验完成后、执行前新立案的调查仍会阻断销毁。"""
    c = seeded
    _past_retention(c)
    _request(c)
    _verify(c, "DR-1", "QA-1", "CNT-1")
    _verify(c, "DR-1", "QA-2", "CNT-1")
    # 核验后新出现调查引用
    c.post("/investigations", json={"code": "INV-NEW", "title": "新调查"})
    c.post("/investigations/INV-NEW/containers", json={"container_code": "CNT-1"})
    r = _execute(c, "DR-1", "QA-1")
    assert r.status_code == 409
    assert r.json()["code"] == "REQUEST_BLOCKED"
    # 申请退回阻断状态，核验记录作废
    req = c.get("/destruction-requests/DR-1").json()
    assert req["status"] == "BLOCKED"
    assert req["verify1_by"] is None
