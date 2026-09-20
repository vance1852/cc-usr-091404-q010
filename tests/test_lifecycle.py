"""生命周期主流程：接收-移动-领用-归还-分装，保管唯一性与数量守恒。"""

from conftest import (aliquot, checkout, do_return, get_container, move,
                      receive, temperature)


def test_receive_places_container_in_single_location(seeded):
    c = seeded
    r = receive(c)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["event_type"] == "RECEIVE"
    assert body["payload"]["quantity_after"] == 100.0

    ctn = get_container(c)
    assert ctn["status"] == "ACTIVE"
    assert ctn["current_quantity"] == 100.0
    assert ctn["holder"] == {"type": "LOCATION", "code": "FREEZER-A", "name": "FREEZER-A"}
    # 库位视角也能看到
    occupants = c.get("/locations/FREEZER-A/containers").json()
    assert [o["code"] for o in occupants] == ["CNT-1"]


def test_move_keeps_exactly_one_custody_location(seeded):
    c = seeded
    receive(c)
    assert move(c).status_code == 201
    ctn = get_container(c)
    assert ctn["holder"]["code"] == "FREEZER-B"
    # 原库位不再持有该容器 —— 任何时刻只有一个有效保管位置
    assert c.get("/locations/FREEZER-A/containers").json() == []
    assert [o["code"] for o in c.get("/locations/FREEZER-B/containers").json()] == ["CNT-1"]


def test_checkout_and_return_conserve_quantity(seeded):
    c = seeded
    receive(c, quantity=100.0)
    assert checkout(c, person="TECH-1").status_code == 201
    ctn = get_container(c)
    assert ctn["holder"] == {"type": "PERSON", "code": "TECH-1", "name": "TECH-1"}
    assert c.get("/locations/FREEZER-A/containers").json() == []
    assert [x["code"] for x in c.get("/persons/TECH-1/containers").json()] == ["CNT-1"]

    r = do_return(c, qty=80.0)
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["consumed_quantity"] == 20.0
    ctn = get_container(c)
    assert ctn["current_quantity"] == 80.0
    assert ctn["holder"]["type"] == "LOCATION"

    # 数量演变：100 -> 100(领用不变) -> 80，守恒：初始 = 剩余 + 消耗
    hist = c.get("/containers/CNT-1/quantity-history").json()
    deltas = [(h["event_type"], h["quantity_before"], h["quantity_after"])
              for h in hist]
    assert ("RECEIVE", 0.0, 100.0) in deltas
    assert ("CHECKOUT", 100.0, 100.0) in deltas
    assert ("RETURN", 100.0, 80.0) in deltas


def test_double_checkout_rejected(seeded):
    c = seeded
    receive(c)
    assert checkout(c).status_code == 201
    r = checkout(c, event_id="evt-co-2", person="QA-1")
    assert r.status_code == 400
    assert "不能重复领用" in r.json()["detail"]


def test_return_without_checkout_rejected(seeded):
    c = seeded
    receive(c)
    r = do_return(c, qty=50.0)
    assert r.status_code == 400
    assert "未处于领用状态" in r.json()["detail"]


def test_return_more_than_checked_out_rejected(seeded):
    c = seeded
    receive(c, quantity=100.0)
    checkout(c)
    r = do_return(c, qty=120.0)
    assert r.status_code == 400
    assert "不守恒" in r.json()["detail"]


def test_return_to_zero_marks_consumed(seeded):
    c = seeded
    receive(c, quantity=50.0)
    checkout(c)
    assert do_return(c, qty=0.0).status_code == 201
    ctn = get_container(c)
    assert ctn["status"] == "CONSUMED"
    assert ctn["current_quantity"] == 0.0
    # 用尽后不能再领用
    assert checkout(c, event_id="evt-co-9").status_code == 400


def test_aliquot_conserves_quantity_and_creates_children(seeded):
    c = seeded
    receive(c, quantity=100.0)
    r = aliquot(c, children=[{"container_code": "CNT-1-A", "quantity": 30.0},
                             {"container_code": "CNT-1-B", "quantity": 20.0}])
    assert r.status_code == 201, r.text
    parent = get_container(c)
    assert parent["current_quantity"] == 50.0
    # 守恒：母剩余 50 + 子 30 + 子 20 = 初始 100
    child_a = get_container(c, "CNT-1-A")
    child_b = get_container(c, "CNT-1-B")
    assert child_a["current_quantity"] == 30.0
    assert child_b["current_quantity"] == 20.0
    assert child_a["parent_code"] == "CNT-1"
    assert child_a["batch_code"] == "LOT-1"
    # 子样初始保管在母容器所在库位
    assert child_a["holder"]["code"] == "FREEZER-A"
    assert parent["current_quantity"] + child_a["current_quantity"] \
        + child_b["current_quantity"] == 100.0


def test_aliquot_exceeding_parent_quantity_rejected(seeded):
    c = seeded
    receive(c, quantity=40.0)
    r = aliquot(c, children=[{"container_code": "CNT-X", "quantity": 41.0}])
    assert r.status_code == 400
    assert "不守恒" in r.json()["detail"] or "超过母容器剩余量" in r.json()["detail"]
    assert c.get("/containers/CNT-X").status_code == 404  # 子容器未残留


def test_aliquot_while_checked_out_rejected(seeded):
    c = seeded
    receive(c)
    checkout(c)
    r = aliquot(c)
    assert r.status_code == 400
    assert "分装" in r.json()["detail"]


def test_events_chain_is_complete_and_verifiable(seeded):
    c = seeded
    receive(c)
    move(c)
    checkout(c)
    do_return(c, qty=90.0)
    temperature(c)
    events = c.get("/containers/CNT-1/events").json()
    types = [e["event_type"] for e in events]
    assert types == ["RECEIVE", "MOVE", "CHECKOUT", "RETURN", "TEMP_EXPOSURE"]
    # 每个事件都带哈希链字段
    assert all(e["hash"] and e["prev_hash"] for e in events)
    chain = c.get("/events/verify-chain").json()
    assert chain["valid"] is True
    assert chain["checked"] == len(events)
