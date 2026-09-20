# 生物样本留样生命周期系统

基于 **FastAPI + SQLAlchemy 2 + SQLite** 的事件溯源（event-sourced）后端，为生物样本留样的
**接收、分装、库位移动、领用、归还、温度暴露、调查保留、法律保留、销毁与更正**
建立不可断裂、不可篡改的事件链。

## 设计要点

### 1. 哈希链事件账本（不可断裂）

- 所有状态变化都只**追加**为一条事件（`app/ledger.py`），任何历史记录都不更新、不删除。
- 每个容器一条独立链：
  `event_hash = SHA256(prev_hash ‖ 容器标识 ‖ seq ‖ 类型 ‖ 时间 ‖ 操作人 ‖ 规范JSON(载荷))`
- 子分装的链以**母容器分装事件的哈希为锚点**（`anchor_event_hash`），父子血缘在密码学上绑定。
- 载荷、序号、前向链接被任何直接改库行为篡改，`GET /chain/verify` 都会立即报告具体断点。

### 2. 容器标识 + 事件标识 + 幂等键抵御重复扫码

- 容器标识全局唯一，重复接收/重复分装/重复子标识一律拒绝。
- 每条事件有不可预测的 `event_uid`（事件标识）。
- 扫码终端可携带 `request_key`；同一键重复提交返回**首次事件**（HTTP 200
  `idempotency_replay: true`），不产生任何新记录——重复扫码是无害的。

### 3. 任何时刻只有一个有效保管位置

投影 `ContainerProjection` 同时只持有一个 `location` 与一个 `custodian`：

- 领用中（有未还量）禁止移动库位、禁止第二人领用；
- 未被领用时不得归还；
- 归还必须回到指定库位，持有人随即清空。

### 4. 数量守恒（领用量 = 剩余量 + 在途量；分装守恒）

- `在库量 + 领用未还量` 始终守恒；`归还量 + 消耗量 ≤ 领用量`，超领、超还直接 422。
- 分装：`母容器减量 = Σ 子分装量`，超量分装拒绝。
- `GET /samples/{id}/timeline` 逐步给出 `delta` 与滚动余额，并标记任何守恒违例。

### 5. 冻融 / 暴露超限自动限制用途

- 累计冻融次数与累计常温暴露时长在事件回放中推导；
- 一旦超过容器自身限度，普通用途领用被拒，仅保留 `INVESTIGATION`、`DESTRUCTION_PREP`。

### 6. 销毁治理

销毁申请（`POST /destruction/request`）逐一检查阻断项：

| 阻断码 | 含义 |
|---|---|
| `RETENTION_PERIOD` | 法规保存期未满 |
| `OPEN_INVESTIGATION` | 被未结稳定性调查引用 |
| `LEGAL_HOLD` | 法律保留 |
| `CHECKED_OUT` | 尚有领用未还 |
| `PENDING_REQUEST` | 已存在进行中的申请 |

之后必须 **双人扫码核验容器身份**（两名不同员工、两次扫码都匹配容器标识；
标签受损时还须双人比对批次记录并显式确认 `identity_attested`），
执行时**再次复查阻断项**（核验后新挂的调查保留仍能拦下销毁）。

标签受损样品也可在销毁流程之外通过 `POST /samples/{id}/identity-verify`
完成双人身份确认并重贴标签，消除"待核验异常"。

### 7. 更正事件恢复真实状态

误操作不能删除或覆盖原记录，只能追加 `CORRECTION`：它指明目标 `seq`、保存原始类型/载荷、
写明更正原因与正确载荷。投影重放时以最后一次更正为准恢复**真实状态**，
错误记录与更正记录同时留在链上，`GET /samples/{id}/corrections` 给出完整轨迹。
销毁（不可逆物理操作）与更正事件本身不可被更正。

`POST /chain/rebuild` 可仅凭账本重建全部投影（含从母容器分装载荷重建子容器登记）。

## 运行

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export SAMPLE_DB_URL="sqlite:///./sample_chain.db"   # 可选，默认值即此
.venv/bin/uvicorn app.main:app --reload
# 交互文档: http://127.0.0.1:8000/docs
```

## 测试

```bash
.venv/bin/python -m pytest          # 58 个测试
```

覆盖：哈希链链接与篡改检测（改载荷/改链接/重排/缺环）、幂等重复扫码、
唯一保管位置、领用/归还/分装守恒、冻融暴露限制、销毁五类阻断、双人核验与错扫拦截、
核验后新增保留的执行时拦截、更正恢复与全链校验、仅凭账本重建，以及题目中
"年度清理低温库两支样品"的端到端场景（`tests/test_scenario_annual_cleanup.py`）。

## 管理员查询

| 接口 | 用途 |
|---|---|
| `GET /samples/{id}` | 当前持有人、库位、数量、保留与受限状态 |
| `GET /samples/{id}/timeline` | 数量演变（滚动余额 + 守恒核对） |
| `GET /samples/{id}/references` | 阻断销毁的调查引用、保留与历史申请 |
| `GET /samples/{id}/blockers` | 销毁资格预检 |
| `GET /samples/{id}/corrections` | 完整更正轨迹（原始记录 ↔ 更正后真相） |
| `GET /search?container_id=&batch_id=&location=` | 按容器/批次/库位查询 |
| `GET /anomalies` | 待核验异常（受损标签、受限、保留、领用未还） |
| `GET /chain/verify` | 全量哈希链完整性校验 |

## 目录结构

```
app/
  models.py      # 事件账本 + 投影表（ORM）
  ledger.py      # 哈希链追加、内容寻址、完整性校验
  projection.py  # 账本重放 → 当前状态（更正在此折叠）
  services.py    # 生命周期命令、守恒校验、销毁资格与双人核验
  queries.py     # 管理员只读查询
  schemas.py     # Pydantic 模型
  main.py        # FastAPI 路由
tests/           # 58 个测试
```
