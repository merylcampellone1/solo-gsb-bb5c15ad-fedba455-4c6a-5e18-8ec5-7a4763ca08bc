"""Database engine/session plumbing (per-app-instance, so tests can isolate)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base


def make_engine(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
