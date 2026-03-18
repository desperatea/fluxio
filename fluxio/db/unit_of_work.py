"""Unit of Work — управление транзакциями.

Все репозитории работают в рамках одной сессии.
Commit/rollback — только через UoW, не через repository.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxio.db.repos.item_repo import ItemRepository
from fluxio.db.repos.price_repo import PriceRepository
from fluxio.db.repos.purchase_repo import PurchaseRepository
from fluxio.db.repos.stats_repo import StatsRepository


class UnitOfWork:
    """Unit of Work — контекстный менеджер транзакций.

    Использование:
        async with uow:
            item = await uow.items.get_by_name("AK-47 | Redline")
            await uow.purchases.create(...)
            await uow.commit()
    """

    items: ItemRepository
    purchases: PurchaseRepository
    prices: PriceRepository
    stats: StatsRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> UnitOfWork:
        self._session = self._session_factory()
        self.items = ItemRepository(self._session)
        self.purchases = PurchaseRepository(self._session)
        self.prices = PriceRepository(self._session)
        self.stats = StatsRepository(self._session)
        return self

    async def __aexit__(self, exc_type: type | None, *_: object) -> None:
        if exc_type:
            await self.rollback()
        await self._session.close()

    @property
    def session(self) -> AsyncSession:
        """Публичный доступ к сессии (для raw-запросов)."""
        return self._session

    async def commit(self) -> None:
        """Зафиксировать все изменения."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Откатить все изменения."""
        await self._session.rollback()
