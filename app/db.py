"""数据库引擎与会话管理。

默认使用 SQLite 文件库，可通过环境变量 SAMPLE_DB_URL 覆盖，
测试通过依赖注入替换 get_db 使用内存库。
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.environ.get("SAMPLE_DB_URL", "sqlite:///./sample_lifecycle.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

Base = declarative_base()


def get_db():
    """FastAPI 依赖：成功提交、异常回滚，保证事件与投影同事务。"""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册
    from app.services.lifecycle import ensure_system_actor

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        ensure_system_actor(db)
        db.commit()
    finally:
        db.close()
