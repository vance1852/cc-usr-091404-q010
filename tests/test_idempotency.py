"""幂等性：容器标识 + 事件标识抵御重复扫码，重复提交不产生副作用。"""

from conftest import (aliquot, checkout, do_return, get_container, move,
                      receive, temperature)


def test_duplicate_receive_returns_same_event(seeded):
    c = seeded
    r1 = receive(c)
    r2 = receive(c)  # 同一扫码流水重发
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["seq"] == r2.json()["seq"]
    ctn = get_container(c)
    assert ctn["current_quantity"] == 100.0  # 数量未翻倍
    assert len(c.get("/containers/CNT-1/events").json()) == 1


def test_same_container_code_different_event_id_conflicts(seeded):
    c = seeded
    receive(c)
    r = receive(c, event_id="evt-recv-OTHER")
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]


def test_duplicate_move_idempotent(seeded):
    c = seeded
    receive(c)
    r1 = move(c)
    r2 = move(c)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["seq"] == r2.json()["seq"]
    assert get_container(c)["holder"]["code"] == "FREEZER-B"
    assert len(c.get("/containers/CNT-1/events").json()) == 2  # RECEIVE + MOVE


def test_duplicate_checkout_and_return_idempotent(seeded):
    c = seeded
    receive(c)
    checkout(c)
    r1 = checkout(c)  # 重复扫码
    assert r1.status_code == 200
    do_return(c, qty=70.0)
    r2 = do_return(c, qty=70.0)  # 重复扫码
    assert r2.status_code == 200
    assert get_container(c)["current_quantity"] == 70.0  # 消耗只记一次


def test_duplicate_aliquot_does_not_recreate_children(seeded):
    c = seeded
    receive(c)
    r1 = aliquot(c)
    r2 = aliquot(c)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["seq"] == r2.json()["seq"]
    parent = get_container(c)
    assert parent["current_quantity"] == 70.0  # 只扣减一次
    children = c.get("/containers?batch_code=LOT-1").json()
    assert sorted(x["code"] for x in children) == ["CNT-1", "CNT-1-A"]


def test_duplicate_temperature_event_idempotent(seeded):
    c = seeded
    receive(c)
    temperature(c)
    r = temperature(c)
    assert r.status_code == 200
    assert get_container(c)["freeze_thaw_count"] == 1  # 冻融次数不重复累计


def test_different_event_ids_are_distinct_events(seeded):
    c = seeded
    receive(c)
    move(c, to="FREEZER-B", event_id="evt-m-1")
    r = move(c, to="FREEZER-A", event_id="evt-m-2")
    assert r.status_code == 201
    assert get_container(c)["holder"]["code"] == "FREEZER-A"
    assert len(c.get("/containers/CNT-1/events").json()) == 3
