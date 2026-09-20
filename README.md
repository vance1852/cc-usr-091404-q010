# 生物样本留样生命周期系统

为生物样本留样的**接收、分装、库位移动、领用、归还、温度暴露、调查保留和销毁**建立不可断裂的事件链。后端基于 FastAPI + SQLAlchemy + SQLite，采用事件溯源设计：事件只增不改，误操作只能通过更正事件恢复真实状态。

## 核心设计

### 事件溯源与不可断裂的事件链
- `Event` 是唯一真相来源，只增不改（append-only）；`Container`/`Custody` 等是由事件流**重放**得出的投影，可随时重建。
- 每个事件携带 `prev_hash` / `hash`（SHA-256 哈希链），任何篡改、插入、删除都会被 `GET /events/verify-chain` 发现。
- 每个事件落库前，系统先把"试探事件"与历史有效事件一起重放，**验证全部不变量通过后才写入**，非法操作在写入前即被拒绝。

### 不变量
| 不变量 | 实现 |
|---|---|
| 抵御重复扫码 | `(container_id, event_id)` 唯一约束 + 幂等返回（重复提交返回 200 与原事件，不产生副作用） |
| 任何时刻只有一个有效保管位置 | `Custody` 以 `container_id` 为主键；重放只产生单一持有人（库位/人员/无） |
| 数量守恒 | 初始量 = 剩余量 + 消耗量（领用-归还核销）+ 子分装量 + 销毁量；分装总量超过母容器剩余量、归还量超过领出量都会被拒绝 |
| 超限自动限制用途 | 冻融次数或累计超温暴露超过限度 → 状态自动变为 `RESTRICTED` 并追加系统事件，此后仅允许 `INVESTIGATION` / `DESTRUCTION_PREP` 用途的领用 |
| 销毁管控 | 申请时检查法规保存期、未结调查、法律保留、待核验异常；两名不同人员分别现场扫码核验容器身份；执行前复检；执行人必须是核验人之一 |
| 误操作只能更正 | 无修改/删除接口；`CORRECTION` 事件引用原事件并携带正确取值，重放时原事件失效但保留在链上，轨迹完整可查 |

### 容器状态机
`ACTIVE → RESTRICTED`（超限自动）/ `PENDING_VERIFICATION`（异常待核验）/ `CONSUMED`（用尽）/ `DESTROYED`（销毁，终态）

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/          # 运行测试（45 个用例）
.venv/bin/python scripts/seed_demo.py      # 运行"年度清理"演示场景
.venv/bin/uvicorn app.main:app --reload    # 启动服务，访问 /docs
```

## 主要 API

### 建档
- `POST /batches` `/locations` `/persons` — 批次（含保存期/冻融/暴露限度策略）、库位、人员

### 事件写入（均需客户端幂等键 `event_id`）
- `POST /containers/receive` — 接收（建立容器，指定批次/数量/库位/保存期）
- `POST /events/move` `/checkout` `/return` — 库位移动 / 领用（到人）/ 归还（核销消耗量）
- `POST /events/aliquot` — 分装（母容器扣减，子容器以 `{event_id}#{子码}` 派生事件建立）
- `POST /events/temperature` — 温度暴露（冻融计数 / 超温暴露累计）
- `POST /events/flag-anomaly` `/resolve-anomaly` — 标记异常（如标签受损）/ 重新扫码确认身份解除
- `POST /events/corrections` — 更正事件（支持 MOVE/CHECKOUT/RETURN/TEMP_EXPOSURE）

### 销毁工作流
- `POST /destruction-requests` — 申请（自动检查并列出阻断引用，状态 `PENDING`/`BLOCKED`）
- `POST /destruction-requests/{id}/recheck` — 阻断解除后复检
- `POST /destruction-requests/{id}/verify` — 双人核验（两人分别扫描容器条码）
- `POST /destruction-requests/{id}/execute` — 执行（执行前复检，追加 DESTROY 事件）
- `POST /destruction-requests/{id}/cancel` — 撤销

### 调查与法律保留
- `POST /investigations` `/investigations/{code}/containers` `/close` — 调查立案、引用容器、结案
- `POST /containers/{code}/legal-holds` `/legal-holds/{id}/release` — 法律保留与解除

### 管理员查询
- `GET /containers/{code}` — 实际持有人、状态、数量、冻融/暴露计数
- `GET /containers/{code}/quantity-history` — 数量演变（每事件前后值与差值）
- `GET /containers/{code}/events` — 完整事件链（含更正标记）
- `GET /containers/{code}/destruction-blockers` — 阻断销毁的引用清单
- `GET /containers/{code}/corrections` — 完整更正轨迹（更正事件 ↔ 原事件配对）
- `GET /batches/{code}/containers` `/locations/{code}/containers` `/persons/{code}/containers` — 按批次/库位/人员检索
- `GET /anomalies/pending` — 待核验异常总览（待身份核验容器、超限容器、待双人核验/被阻断的销毁申请）
- `GET /events/verify-chain` — 全库事件哈希链完整性校验

## 目录结构

```
app/
  main.py                 # FastAPI 路由
  models.py               # ORM：Event(哈希链) / Container / Custody / 销毁申请等
  schemas.py              # 请求模型
  services/
    lifecycle.py          # 核心：事件应用、状态重放、不变量强制、更正
    destruction.py        # 销毁工作流：法规检查、双人核验、执行
    queries.py            # 查询投影与审计视图
    chain.py              # 哈希链
tests/                    # 45 个用例：生命周期/幂等/守恒/限度/销毁/更正/查询/场景
scripts/seed_demo.py      # 年度清理演示场景
```
