"""Фабрика асинхронных сессий SQLAlchemy."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fluxio.config import config


def create_engine(url: str | None = None) -> AsyncEngine:
    """Создать async engine."""
    return create_async_engine(
        url or config.env.database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
    )


# Async engine — единственный экземпляр на весь процесс
engine: AsyncEngine = create_engine()

# Фабрика сессий
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
