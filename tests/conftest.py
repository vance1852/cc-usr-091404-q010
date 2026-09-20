"""测试基座：每个用例独立内存库 + 常用建档/事件辅助函数。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.services.lifecycle import ensure_system_actor


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    db = TestingSession()
    ensure_system_actor(db)
    db.commit()
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded(client: TestClient):
    """标准档案：批次/库位/人员。"""
    assert client.post("/batches", json={
        "code": "LOT-1", "name": "原液批次", "retention_years": 1.0,
        "max_freeze_thaw": 2, "max_exposure_minutes": 60,
        "storage_threshold_c": -20.0}).status_code == 201
    for loc in ("FREEZER-A", "FREEZER-B", "STAGING-1"):
        assert client.post("/locations", json={
            "code": loc, "name": loc, "kind": "STAGING" if "STAGING" in loc else "FREEZER"
        }).status_code == 201
    for p in ("ADMIN", "QA-1", "QA-2", "TECH-1"):
        assert client.post("/persons", json={
            "code": p, "name": p, "role": "QA" if p.startswith("QA") else "ADMIN"
        }).status_code == 201
    return client


# ---------------------------------------------------------------- 辅助函数

def receive(client, code="CNT-1", event_id="evt-recv-1", quantity=100.0,
            location="FREEZER-A", batch="LOT-1", actor="ADMIN", **kw):
    return client.post("/containers/receive", json={
        "event_id": event_id, "container_code": code, "batch_code": batch,
        "quantity": quantity, "unit": "mL", "location_code": location,
        "actor_code": actor, **kw})


def move(client, code="CNT-1", to="FREEZER-B", event_id="evt-move-1", actor="ADMIN"):
    return client.post("/events/move", json={
        "event_id": event_id, "container_code": code,
        "to_location_code": to, "actor_code": actor})


def checkout(client, code="CNT-1", person="TECH-1", event_id="evt-co-1",
             purpose="ANALYSIS"):
    return client.post("/events/checkout", json={
        "event_id": event_id, "container_code": code,
        "person_code": person, "purpose": purpose})


def do_return(client, code="CNT-1", to="FREEZER-A", qty=80.0,
              event_id="evt-ret-1", actor="TECH-1"):
    return client.post("/events/return", json={
        "event_id": event_id, "container_code": code, "to_location_code": to,
        "returned_quantity": qty, "actor_code": actor})


def aliquot(client, parent="CNT-1", children=None, event_id="evt-ali-1", actor="ADMIN"):
    if children is None:
        children = [{"container_code": "CNT-1-A", "quantity": 30.0}]
    return client.post("/events/aliquot", json={
        "event_id": event_id, "parent_code": parent,
        "children": children, "actor_code": actor})


def temperature(client, code="CNT-1", temp=-15.0, minutes=30.0, thawed=True,
                event_id="evt-temp-1", actor="TECH-1"):
    return client.post("/events/temperature", json={
        "event_id": event_id, "container_code": code, "temperature_c": temp,
        "duration_minutes": minutes, "thawed": thawed, "actor_code": actor})


def get_container(client, code="CNT-1"):
    r = client.get(f"/containers/{code}")
    assert r.status_code == 200, r.text
    return r.json()
