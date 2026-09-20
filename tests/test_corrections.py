"""更正事件：误操作只能通过更正恢复真实状态，历史不可变、轨迹可查。"""

from conftest import (checkout, do_return, get_container, move, receive,
                      temperature)


def _correct(c, container, corrects, event_id, payload, reason="录入错误"):
    return c.post("/events/corrections", json={
        "event_id": event_id, "container_code": container,
        "corrects_event_id": corrects, "actor_code": "QA-1",
        "reason": reason, "corrected_payload": payload})


def test_correct_wrong_move_restores_true_location(seeded):
    c = seeded
    receive(c)
    move(c, to="STAGING-1", event_id="evt-move-wrong")  # 误移到暂存箱
    assert get_container(c)["holder"]["code"] == "STAGING-1"

    r = _correct(c, "CNT-1", "evt-move-wrong", "evt-corr-1",
                 {"to_location_code": "FREEZER-B"})
    assert r.status_code == 201, r.text
    assert r.json()["event_type"] == "CORRECTION"

    ctn = get_container(c)
    assert ctn["holder"]["code"] == "FREEZER-B"  # 真实状态恢复
    assert c.get("/locations/STAGING-1/containers").json() == []

    # 原事件仍在链上，但标记为已被更正
    events = c.get("/containers/CNT-1/events").json()
    wrong = next(e for e in events if e["event_id"] == "evt-move-wrong")
    assert wrong["corrected_by_event_id"] == "evt-corr-1"
    assert events[-1]["event_type"] == "CORRECTION"

    # 更正轨迹可查
    trail = c.get("/containers/CNT-1/corrections").json()
    assert len(trail) == 1
    assert trail[0]["target"]["event_id"] == "evt-move-wrong"
    assert trail[0]["correction"]["event_id"] == "evt-corr-1"
    assert trail[0]["reason"] == "录入错误"

    # 哈希链依然完整
    assert c.get("/events/verify-chain").json()["valid"] is True


def test_correct_return_quantity_restores_conservation(seeded):
    c = seeded
    receive(c, quantity=100.0)
    checkout(c)
    do_return(c, qty=95.0)  # 误录：实际归还 80
    assert get_container(c)["current_quantity"] == 95.0

    r = _correct(c, "CNT-1", "evt-ret-1", "evt-corr-ret",
                 {"to_location_code": "FREEZER-A", "returned_quantity": 80.0})
    assert r.status_code == 201, r.text
    ctn = get_container(c)
    assert ctn["current_quantity"] == 80.0  # 守恒恢复：100 - 20 消耗

    hist = c.get("/containers/CNT-1/quantity-history").json()
    corrected = next(h for h in hist if h["event_id"] == "evt-corr-ret")
    assert corrected["quantity_after"] == 80.0
    original = next(h for h in hist if h["event_id"] == "evt-ret-1")
    assert original["corrected"] is True


def test_correct_temperature_event_lifts_restriction(seeded):
    c = seeded
    receive(c)
    temperature(c, event_id="t1")
    temperature(c, event_id="t2")
    temperature(c, event_id="t3")  # 第 3 次冻融 -> RESTRICTED
    assert get_container(c)["status"] == "RESTRICTED"

    # t3 系误录（实际未冻融），更正后限制解除
    r = _correct(c, "CNT-1", "t3", "evt-corr-t3",
                 {"temperature_c": -80.0, "duration_minutes": 5.0, "thawed": False})
    assert r.status_code == 201
    ctn = get_container(c)
    assert ctn["freeze_thaw_count"] == 2
    assert ctn["status"] == "ACTIVE"
    types = [e["event_type"] for e in c.get("/containers/CNT-1/events").json()]
    assert "RESTRICTION_LIFTED" in types


def test_correction_violating_invariants_rejected(seeded):
    c = seeded
    receive(c, quantity=100.0)
    checkout(c)
    do_return(c, qty=90.0)
    # 更正为归还 120 > 领出 100，破坏守恒 -> 拒绝
    r = _correct(c, "CNT-1", "evt-ret-1", "evt-corr-bad",
                 {"to_location_code": "FREEZER-A", "returned_quantity": 120.0})
    assert r.status_code == 400
    assert "不守恒" in r.json()["detail"]
    assert get_container(c)["current_quantity"] == 90.0  # 状态未被破坏


def test_double_correction_rejected(seeded):
    c = seeded
    receive(c)
    move(c, to="STAGING-1", event_id="evt-move-wrong")
    assert _correct(c, "CNT-1", "evt-move-wrong", "evt-corr-1",
                    {"to_location_code": "FREEZER-B"}).status_code == 201
    r = _correct(c, "CNT-1", "evt-move-wrong", "evt-corr-2",
                 {"to_location_code": "FREEZER-A"})
    assert r.status_code == 409
    assert r.json()["code"] == "ALREADY_CORRECTED"


def test_uncorrectable_event_types_rejected(seeded):
    c = seeded
    receive(c)
    r = _correct(c, "CNT-1", "evt-recv-1", "evt-corr-recv", {})
    assert r.status_code == 400
    assert r.json()["code"] == "NOT_CORRECTABLE"


def test_correction_requires_reason_and_existing_target(seeded):
    c = seeded
    receive(c)
    move(c, event_id="evt-move-1")
    r = c.post("/events/corrections", json={
        "event_id": "evt-corr-x", "container_code": "CNT-1",
        "corrects_event_id": "evt-move-1", "actor_code": "QA-1",
        "reason": "", "corrected_payload": {"to_location_code": "FREEZER-B"}})
    assert r.status_code == 400
    r = _correct(c, "CNT-1", "evt-no-such", "evt-corr-y",
                 {"to_location_code": "FREEZER-B"})
    assert r.status_code == 404


def test_correction_is_idempotent(seeded):
    c = seeded
    receive(c)
    move(c, to="STAGING-1", event_id="evt-move-wrong")
    r1 = _correct(c, "CNT-1", "evt-move-wrong", "evt-corr-1",
                  {"to_location_code": "FREEZER-B"})
    r2 = _correct(c, "CNT-1", "evt-move-wrong", "evt-corr-1",
                  {"to_location_code": "FREEZER-B"})
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["seq"] == r2.json()["seq"]
