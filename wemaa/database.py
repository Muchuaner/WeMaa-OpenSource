"""WeMaa 自有业务表；生产可沿用已有定时任务表而无需复制或重放。"""
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from wemaa.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    settings.WEMAA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False}} if settings.WEMAA_DATABASE_URL.startswith("sqlite:") else {}
    return create_engine(settings.WEMAA_DATABASE_URL, pool_pre_ping=True, **kwargs)


def session_factory():
    return sessionmaker(bind=engine(), expire_on_commit=False)


def get_db():
    with session_factory()() as session:
        yield session


def initialize():
    from wemaa import models  # noqa: F401
    Base.metadata.create_all(engine())
