"""Shared async engine + session factory.

Settings are read when `get_engine()` is first called, never at import: importing this
module (or anything that imports it) must not require DATABASE_URL.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL must be set")
    return url


def get_engine() -> AsyncEngine:
    """The process-wide engine, created on first use from DATABASE_URL."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(database_url(), pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on error. Write paths never commit;
    the caller owns the transaction (same discipline as app/auth/repository.py)."""
    async with get_sessionmaker()() as session, session.begin():
        yield session


async def dispose() -> None:
    """Close the pool (app shutdown, tests). A later `get_engine()` builds a fresh one."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
