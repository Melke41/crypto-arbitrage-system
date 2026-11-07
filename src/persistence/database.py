"""Async database engine and session factory management."""

from __future__ import annotations

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.persistence.models import Base


def create_db_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    """Create asynchronous SQLAlchemy engine."""
    # SQLite memory requires connect_args check_same_thread=False
    if "sqlite" in database_url:
        return create_async_engine(
            database_url,
            echo=echo,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create sessionmaker for async database transactions."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def init_db(engine: AsyncEngine) -> None:
    """Create all registered database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db(engine: AsyncEngine) -> None:
    """Drop all tables (primarily for clean teardown in tests)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
