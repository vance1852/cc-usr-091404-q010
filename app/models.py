"""ORM 模型。

设计要点：
- Event 是唯一的真相来源，只增不改（append-only），带 SHA-256 哈希链防断裂、防篡改；
- Container / Custody 等是投影（projection），由事件流重放得出，可随时重建；
- (container_id, event_id) 唯一约束抵御重复扫码；
- Custody 以 container_id 为主键，保证任何时刻一个容器只有一个有效保管位置。
"""
from __future__ import annotations

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Batch(Base):
    """批次（如一批原液），携带默认的保存期与冻融/暴露限度策略。"""
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    retention_years: Mapped[float] = mapped_column(Float, default=1.0)      # 法规保存期（年）
    max_freeze_thaw: Mapped[int] = mapped_column(Integer, default=3)        # 允许冻融次数
    max_exposure_minutes: Mapped[float] = mapped_column(Float, default=120)  # 允许累计超限暴露（分钟）
    storage_threshold_c: Mapped[float] = mapped_column(Float, default=-20.0)  # 储存温度阈值（℃）

    containers: Mapped[list["Container"]] = relationship(back_populates="batch")


class Location(Base):
    """库位：冷库 / 货架 / 盒位 / 暂存箱等。"""
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(32), default="FREEZER")  # FREEZER/SHELF/BOX/STAGING...


class Person(Base):
    """人员：操作者、持有人、核验人。SYSTEM 为系统自动 Actor。"""
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), default="TECHNICIAN")  # ADMIN/QA/TECHNICIAN/SYSTEM


class Container(Base):
    """容器（留样/分装子样）。数量为投影字段，由事件流重放维护。"""
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 容器条码
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("containers.id"), nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="mL")
    initial_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    current_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    # ACTIVE / RESTRICTED / PENDING_VERIFICATION / CONSUMED / DESTROYED
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    freeze_thaw_count: Mapped[int] = mapped_column(Integer, default=0)
    exposure_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    anomaly_type: Mapped[str | None] = mapped_column(String(32), nullable=True)   # 待核验异常类型
    anomaly_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_until: Mapped[Date | None] = mapped_column(Date, nullable=True)     # 法规保存期截止日
    max_freeze_thaw: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 容器级覆盖
    max_exposure_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    destroyed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime)

    batch: Mapped[Batch] = relationship(back_populates="containers")
    parent: Mapped["Container | None"] = relationship(remote_side=[id])


class Custody(Base):
    """当前保管位置投影：主键即 container_id —— 任何时刻只有一个有效保管位置。"""
    __tablename__ = "custody"

    container_id: Mapped[int] = mapped_column(ForeignKey("containers.id"), primary_key=True)
    holder_type: Mapped[str] = mapped_column(String(16), default="NONE")  # LOCATION / PERSON / NONE
    holder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    since_event_seq: Mapped[int] = mapped_column(Integer, default=0)      # 最近一次变更保管的事件序号
    updated_at: Mapped[DateTime] = mapped_column(DateTime)


class Event(Base):
    """不可变事件。哈希链：hash = sha256(prev_hash + canonical(字段))。"""
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("container_id", "event_id", name="uq_container_event"),  # 抵御重复扫码
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128))          # 客户端幂等键（扫码流水号）
    container_id: Mapped[int] = mapped_column(ForeignKey("containers.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    occurred_at: Mapped[DateTime] = mapped_column(DateTime)     # 业务发生时间（客户端可指定）
    recorded_at: Mapped[DateTime] = mapped_column(DateTime)     # 服务端落库时间
    payload: Mapped[dict] = mapped_column(JSON, default=dict)   # 类型相关数据 + 数量前后值
    corrects_event_seq: Mapped[int | None] = mapped_column(ForeignKey("events.seq"), nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))

    container: Mapped[Container] = relationship()
    actor: Mapped[Person] = relationship()


class Investigation(Base):
    """调查（如稳定性调查）。OPEN 状态会阻断所引用容器的销毁。"""
    __tablename__ = "investigations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN / CLOSED
    opened_at: Mapped[DateTime] = mapped_column(DateTime)
    closed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)


class InvestigationContainer(Base):
    """调查与容器的引用关系（调查保留）。"""
    __tablename__ = "investigation_containers"
    __table_args__ = (UniqueConstraint("investigation_id", "container_id", name="uq_inv_container"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(ForeignKey("investigations.id"))
    container_id: Mapped[int] = mapped_column(ForeignKey("containers.id"))
    linked_at: Mapped[DateTime] = mapped_column(DateTime)
    released_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    investigation: Mapped[Investigation] = relationship()
    container: Mapped[Container] = relationship()


class LegalHold(Base):
    """法律保留。未解除时阻断销毁。"""
    __tablename__ = "legal_holds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    container_id: Mapped[int] = mapped_column(ForeignKey("containers.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    placed_by: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    placed_at: Mapped[DateTime] = mapped_column(DateTime)
    released_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    released_by: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)


class DestructionRequest(Base):
    """销毁申请：法规检查 -> 双人核验容器身份 -> 执行。"""
    __tablename__ = "destruction_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)  # 客户端幂等键
    container_id: Mapped[int] = mapped_column(ForeignKey("containers.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    requested_at: Mapped[DateTime] = mapped_column(DateTime)
    # PENDING(待核验) / BLOCKED(被阻断) / VERIFIED(已双人核验) / EXECUTED / CANCELLED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    blockers: Mapped[list] = mapped_column(JSON, default=list)  # 阻断销毁的引用清单
    verify1_by: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    verify1_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    verify2_by: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    verify2_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    executed_by: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    executed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    destroy_event_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    container: Mapped[Container] = relationship()
