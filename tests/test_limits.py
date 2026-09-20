"""冻融次数与温度暴露限度：超限自动限制用途，更正后可恢复。"""

from conftest import (checkout, get_container, receive, temperature)


def test_freeze_thaw_limit_auto_restricts(seeded):
    c = seeded  # 批次限度：冻融 2 次
    receive(c)
    temperature(c, event_id="t1")
    temperature(c, event_id="t2")
    assert get_container(c)["status"] == "ACTIVE"
    r = temperature(c, event_id="t3")  # 第 3 次冻融，超限
    assert r.status_code == 201
    ctn = get_container(c)
    assert ctn["freeze_thaw_count"] == 3
    assert ctn["status"] == "RESTRICTED"
    # 系统自动追加限制事件，审计可见
    types = [e["event_type"] for e in c.get("/containers/CNT-1/events").json()]
    assert "RESTRICTION_APPLIED" in types


def test_exposure_minutes_limit_auto_restricts(seeded):
    c = seeded  # 批次限度：60 分钟超限暴露
    receive(c)
    temperature(c, temp=-10.0, minutes=50.0, thawed=False, event_id="t1")
    assert get_container(c)["status"] == "ACTIVE"
    temperature(c, temp=-10.0, minutes=20.0, thawed=False, event_id="t2")
    ctn = get_container(c)
    assert ctn["exposure_minutes"] == 70.0
    assert ctn["status"] == "RESTRICTED"


def test_below_threshold_temperature_not_counted(seeded):
    c = seeded
    receive(c)
    temperature(c, temp=-80.0, minutes=999.0, thawed=False, event_id="t1")
    ctn = get_container(c)
    assert ctn["exposure_minutes"] == 0.0
    assert ctn["status"] == "ACTIVE"


def test_restricted_container_purpose_limited(seeded):
    c = seeded
    receive(c)
    for i in range(3):
        temperature(c, event_id=f"t{i}")
    assert get_container(c)["status"] == "RESTRICTED"
    # 放行用途被拒绝
    r = checkout(c, event_id="co-rel", purpose="RELEASE")
    assert r.status_code == 400
    assert "受限" in r.json()["detail"]
    # 常规分析同样被拒绝
    assert checkout(c, event_id="co-ana", purpose="ANALYSIS").status_code == 400
    # 调查用途允许
    assert checkout(c, event_id="co-inv", purpose="INVESTIGATION").status_code == 201


def test_container_level_limit_overrides_batch(seeded):
    c = seeded
    receive(c, code="CNT-9", event_id="evt-recv-9", max_freeze_thaw=5)
    for i in range(3):
        # 低于阈值的温度不计暴露，仅累计冻融次数
        temperature(c, code="CNT-9", temp=-80.0, event_id=f"t9-{i}")
    ctn = get_container(c, "CNT-9")
    assert ctn["freeze_thaw_count"] == 3
    assert ctn["status"] == "ACTIVE"  # 容器级限度 5 未超（批次限度 2 已被覆盖）
