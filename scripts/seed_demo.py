#!/usr/bin/env python3
"""演示脚本：还原"年度清理低温库"场景。

  - STOCK-001：待销毁原液留样，仍被稳定性调查引用 -> 销毁被阻断
  - LBL-002：标签受损样品，已移入暂存箱 -> 待身份核验，销毁被阻断

运行：.venv/bin/python scripts/seed_demo.py
脚本使用独立的演示数据库文件 demo_lifecycle.db（可删除重跑）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SAMPLE_DB_URL"] = "sqlite:///./demo_lifecycle.db"

if os.path.exists("demo_lifecycle.db"):
    os.remove("demo_lifecycle.db")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def show(title, data):
    print(f"\n=== {title} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    with TestClient(app) as c:
        # ---- 建档 -------------------------------------------------------
        c.post("/batches", json={"code": "LOT-2024-001", "name": "原液批次",
                                 "retention_years": 1.0, "max_freeze_thaw": 3,
                                 "max_exposure_minutes": 120})
        c.post("/locations", json={"code": "FREEZER-A", "name": "低温库 A", "kind": "FREEZER"})
        c.post("/locations", json={"code": "STAGING-1", "name": "暂存箱 1", "kind": "STAGING"})
        for p, role in [("ADMIN", "ADMIN"), ("QA-1", "QA"), ("QA-2", "QA")]:
            c.post("/persons", json={"code": p, "name": p, "role": role})

        # ---- 两支留样 ----------------------------------------------------
        c.post("/containers/receive", json={
            "event_id": "scan-0001", "container_code": "STOCK-001",
            "batch_code": "LOT-2024-001", "quantity": 500, "location_code": "FREEZER-A",
            "actor_code": "ADMIN", "retention_until": "2025-12-31"})
        c.post("/investigations", json={"code": "INV-STAB-2026", "title": "稳定性调查"})
        c.post("/investigations/INV-STAB-2026/containers",
               json={"container_code": "STOCK-001"})

        c.post("/containers/receive", json={
            "event_id": "scan-0002", "container_code": "LBL-002",
            "batch_code": "LOT-2024-001", "quantity": 50, "location_code": "FREEZER-A",
            "actor_code": "ADMIN", "retention_until": "2025-12-31"})
        c.post("/events/flag-anomaly", json={
            "event_id": "scan-0003", "container_code": "LBL-002",
            "anomaly_type": "LABEL_DAMAGED", "note": "冷链标签受潮", "actor_code": "ADMIN"})
        c.post("/events/move", json={
            "event_id": "scan-0004", "container_code": "LBL-002",
            "to_location_code": "STAGING-1", "actor_code": "ADMIN"})

        # ---- 年度清理：按清单申请销毁，双双被阻断 --------------------------
        r1 = c.post("/destruction-requests", json={
            "request_id": "DR-STOCK", "container_code": "STOCK-001",
            "reason": "年度清理", "requested_by": "ADMIN"}).json()
        show("销毁申请 DR-STOCK（被未结调查阻断）", r1)

        r2 = c.post("/destruction-requests", json={
            "request_id": "DR-LBL", "container_code": "LBL-002",
            "reason": "年度清理", "requested_by": "ADMIN"}).json()
        show("销毁申请 DR-LBL（标签受损待核验阻断）", r2)

        show("待核验异常总览", c.get("/anomalies/pending").json())

        # ---- 标签样品：重新扫码确认身份 -> 双人核验 -> 销毁 -----------------
        c.post("/events/resolve-anomaly", json={
            "event_id": "scan-0005", "container_code": "LBL-002",
            "confirmed_container_code": "LBL-002", "actor_code": "QA-1"})
        c.post("/destruction-requests/DR-LBL/recheck")
        c.post("/destruction-requests/DR-LBL/verify",
               json={"person_code": "QA-1", "scanned_container_code": "LBL-002"})
        r = c.post("/destruction-requests/DR-LBL/verify",
                   json={"person_code": "QA-2", "scanned_container_code": "LBL-002"})
        show("双人核验完成", r.json())
        r = c.post("/destruction-requests/DR-LBL/execute",
                   json={"person_code": "QA-2", "event_id": "scan-0006"})
        show("LBL-002 已销毁", r.json())

        # ---- 原液留样：调查未结案，依旧无法销毁 -----------------------------
        r = c.post("/destruction-requests/DR-STOCK/verify",
                   json={"person_code": "QA-1", "scanned_container_code": "STOCK-001"})
        show("调查未结案，核验被拒绝", r.json())
        c.post("/investigations/INV-STAB-2026/close")
        r = c.post("/destruction-requests/DR-STOCK/recheck")
        show("调查结案后复检通过", r.json())

        # ---- 审计视图 ------------------------------------------------------
        show("LBL-002 数量演变", c.get("/containers/LBL-002/quantity-history").json())
        show("LBL-002 完整事件链", c.get("/containers/LBL-002/events").json())
        show("事件链完整性校验", c.get("/events/verify-chain").json())

        print("\n演示数据库已写入 demo_lifecycle.db，"
              "可运行 .venv/bin/uvicorn app.main:app --reload 后访问 /docs 继续探索。")


if __name__ == "__main__":
    main()
