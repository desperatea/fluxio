"""Репозиторий цен. НЕ делает commit — только через UoW."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from fluxio.db.models import ApiLog, PriceHistory


class PriceRepository:
    """CRUD операции для истории цен."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_snapshot(
        self,
        market_hash_name: str,
        platform: str,
        price: float,
        currency: str,
        sales_count: int | None = None,
    ) -> PriceHistory:
        """Сохранить снэпшот цены."""
        record = PriceHistory(
            market_hash_name=market_hash_name,
            platform=platform,
            price=price,
            currency=currency,
            sales_count=sales_count,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_latest(
        self,
        market_hash_name: str,
        platform: str = "steam",
    ) -> PriceHistory | None:
        """Получить последнюю запись цены."""
        result = await self._session.execute(
            select(PriceHistory)
            .where(PriceHistory.market_hash_name == market_hash_name)
            .where(PriceHistory.platform == platform)
            .order_by(PriceHistory.recorded_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def cleanup_old_records(self, days: int = 90) -> int:
        """Удалить записи price_history и api_logs старше N дней."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        result1 = await self._session.execute(
            delete(PriceHistory).where(PriceHistory.recorded_at < cutoff)
        )
        result2 = await self._session.execute(
            delete(ApiLog).where(ApiLog.logged_at < cutoff)
        )
        await self._session.flush()

        return (result1.rowcount or 0) + (result2.rowcount or 0)
