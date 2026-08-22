from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.core.config import settings


SessionFactory = Callable[[], Session]


def create_database_engine(database_url: str | None = None) -> Engine:
    """Create the synchronous SQLAlchemy engine used by the persistence layer."""

    return create_engine(database_url or settings.database_url, future=True)


def create_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Create a session factory with expire-on-commit disabled for audit writers."""

    return sessionmaker(bind=engine or create_database_engine(), expire_on_commit=False)


SessionLocal = create_session_factory()
