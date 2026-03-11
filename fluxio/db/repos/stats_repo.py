"""Репозиторий статистики. НЕ делает commit — только через UoW."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fluxio.db.models import Blacklist, DailyStat, Purchase


class StatsRepository:
    """CRUD операции для статистики и чёрного списка."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def update_daily(self, stat_date: date | None = None) -> DailyStat:
        """Обновить/создать ежедневную статистику."""
        stat_date = stat_date or date.today()

        result = await self._session.execute(
            select(DailyStat).where(DailyStat.date == stat_date)
        )
        stat = result.scalar_one_or_none()

        purchases_result = await self._session.execute(
            select(
                func.count().label("count"),
                func.coalesce(func.sum(Purchase.price_cny), 0).label("spent"),
                func.coalesce(func.sum(Purchase.steam_price_usd), 0).label("value"),
            )
            .where(Purchase.dry_run == False)
            .where(Purchase.status.in_(["pending", "success"]))
            .where(func.date(Purchase.purchased_at) == stat_date)
        )
        row = purchases_result.one()

        if stat is None:
            stat = DailyStat(date=stat_date)
            self._session.add(stat)

        stat.items_purchased = row.count
        stat.total_spent_cny = float(row.spent)
        stat.potential_value_usd = float(row.value)
        stat.potential_profit_usd = float(row.value) - float(row.spent)

        await self._session.flush()
        return stat

    async def get_daily(self, stat_date: date | None = None) -> DailyStat | None:
        """Получить статистику за день."""
        stat_date = stat_date or date.today()
        result = await self._session.execute(
            select(DailyStat).where(DailyStat.date == stat_date)
        )
        return result.scalar_one_or_none()

    # --- Чёрный список ---

    async def get_blacklist_names(self) -> set[str]:
        """Получить множество заблокированных market_hash_name."""
        result = await self._session.execute(select(Blacklist.market_hash_name))
        return set(result.scalars().all())

    async def add_to_blacklist(
        self,
        market_hash_name: str,
        reason: str | None = None,
        steam_url: str | None = None,
    ) -> Blacklist:
        """Добавить предмет в чёрный список."""
        item = Blacklist(
            market_hash_name=market_hash_name,
            reason=reason,
            steam_url=steam_url,
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def remove_from_blacklist(self, market_hash_name: str) -> bool:
        """Удалить предмет из чёрного списка. Возвращает True если был найден."""
        result = await self._session.execute(
            select(Blacklist).where(Blacklist.market_hash_name == market_hash_name)
        )
        item = result.scalar_one_or_none()
        if item:
            await self._session.delete(item)
            await self._session.flush()
            return True
        return False
