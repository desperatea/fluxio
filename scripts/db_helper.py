"""Общий модуль подключения к PostgreSQL для скриптов.

Все скрипты используют этот модуль вместо прямого подключения к SQLite.
Подключается к PostgreSQL в Docker (или localhost).

Использование:
    from db_helper import engine, async_session, init_db
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Корень проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fluxio.db.models import Base

# PostgreSQL URL из переменных окружения
PG_USER = os.getenv("POSTGRES_USER", "bot")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "secret")
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = os.getenv("POSTGRES_PORT", "5432")
PG_DB = os.getenv("POSTGRES_DB", "c5game_bot")

DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASS}"
    f"@{PG_HOST}:{PG_PORT}/{PG_DB}"
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создать все таблицы в PostgreSQL (если не существуют)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
