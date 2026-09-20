from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_DB_URL = "sqlite:///" + str(Path(__file__).resolve().parent.parent / "sample_chain.db")


class Base(DeclarativeBase):
    pass


def make_engine(db_url: str = DEFAULT_DB_URL) -> Engine:
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, connect_args=connect_args, future=True)


def init_db(engine) -> None:
    import importlib

    importlib.import_module("app.models")  # register mappers before create_all
    Base.metadata.create_all(engine)


def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@lru_cache(maxsize=8)
def _cached_engine(db_url: str) -> Engine:
    engine = make_engine(db_url)
    init_db(engine)
    return engine


def get_session() -> Iterator[Session]:  # FastAPI dependency
    from .main import build_settings

    settings = build_settings()
    factory = session_factory(_cached_engine(settings.db_url))
    with factory() as session:
        yield session
