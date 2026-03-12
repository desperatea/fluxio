"""Репозиторий покупок. НЕ делает commit — только через UoW."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fluxio.db.models import ActiveOrder, Purchase


class PurchaseRepository:
    """CRUD операции для покупок и активных заказов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        product_id: str,
        market_hash_name: str,
        price_usd: float,
        status: str = "pending",
        steam_price_usd: float | None = None,
        discount_percent: float | None = None,
        dry_run: bool = False,
        api_response: dict[str, Any] | None = None,
        order_id: str | None = None,
    ) -> Purchase:
        """Создать запись о покупке."""
        purchase = Purchase(
            product_id=product_id,
            order_id=order_id,
            market_hash_name=market_hash_name,
            price_usd=price_usd,
            steam_price_usd=steam_price_usd,
            discount_percent=discount_percent,
            status=status,
            dry_run=dry_run,
            api_response=api_response,
        )
        self._session.add(purchase)
        await self._session.flush()
        return purchase

    async def exists(self, product_id: str) -> bool:
        """Проверить, был ли product_id уже куплен."""
        result = await self._session.execute(
            select(func.count())
            .select_from(Purchase)
            .where(Purchase.product_id == product_id)
        )
        return result.scalar_one() > 0

    async def get_today_spent(self) -> float:
        """Получить сумму трат за сегодня."""
        today = date.today()
        result = await self._session.execute(
            select(func.coalesce(func.sum(Purchase.price_usd), 0))
            .where(Purchase.dry_run == False)
            .where(Purchase.status.in_(["pending", "success"]))
            .where(func.date(Purchase.purchased_at) == today)
        )
        return float(result.scalar_one())

    async def get_same_item_count_24h(self, market_hash_name: str) -> int:
        """Получить количество покупок одного предмета за 24 часа."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await self._session.execute(
            select(func.count())
            .select_from(Purchase)
            .where(Purchase.market_hash_name == market_hash_name)
            .where(Purchase.dry_run == False)
            .where(Purchase.status.in_(["pending", "success"]))
            .where(Purchase.purchased_at >= cutoff)
        )
        return result.scalar_one()

    async def get_purchases_last_hour(self) -> int:
        """Количество покупок за последний час (для kill switch)."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        result = await self._session.execute(
            select(func.count())
            .select_from(Purchase)
            .where(Purchase.dry_run == False)
            .where(Purchase.purchased_at >= cutoff)
        )
        return result.scalar_one()

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
        dry_run: bool | None = None,
    ) -> Sequence[Purchase]:
        """Получить покупки с пагинацией."""
        q = select(Purchase).order_by(Purchase.purchased_at.desc())
        if dry_run is not None:
            q = q.where(Purchase.dry_run == dry_run)
        q = q.offset(offset).limit(limit)
        result = await self._session.execute(q)
        return result.scalars().all()

    # --- Активные заказы ---

    async def save_active_order(
        self,
        order_id: str,
        purchase_id: int | None = None,
        status: str = "pending",
    ) -> ActiveOrder:
        """Сохранить активный заказ."""
        order = ActiveOrder(
            order_id=order_id,
            purchase_id=purchase_id,
            status=status,
        )
        self._session.add(order)
        await self._session.flush()
        return order

    async def get_stale_orders(self, stale_minutes: int = 10) -> Sequence[ActiveOrder]:
        """Получить зависшие заказы (старше N минут)."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        result = await self._session.execute(
            select(ActiveOrder)
            .where(ActiveOrder.status == "pending")
            .where(ActiveOrder.created_at < cutoff)
        )
        return result.scalars().all()
