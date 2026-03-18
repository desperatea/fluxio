"""Репозиторий предметов. НЕ делает commit — только через UoW."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fluxio.db.models import Item, SaleListing


class ItemRepository:
    """CRUD операции для каталога предметов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_name(self, market_hash_name: str) -> Item | None:
        """Найти предмет по market_hash_name."""
        stmt = select(Item).where(Item.market_hash_name == market_hash_name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all(self) -> Sequence[Item]:
        """Получить все предметы из каталога."""
        result = await self._session.execute(
            select(Item).order_by(Item.market_hash_name)
        )
        return result.scalars().all()

    async def get_all_paginated(
        self, offset: int = 0, limit: int = 50
    ) -> Sequence[Item]:
        """Получить предметы с пагинацией."""
        result = await self._session.execute(
            select(Item)
            .order_by(Item.market_hash_name)
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def count(self) -> int:
        """Общее количество предметов."""
        result = await self._session.execute(select(func.count(Item.id)))
        return result.scalar_one()

    async def get_candidates(
        self,
        min_discount: float,
        max_price: float,
        freshness_minutes: int = 30,
    ) -> list[Item]:
        """Кандидаты на покупку: есть steam-цена, дисконт >= порога."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=freshness_minutes)
        stmt = (
            select(Item)
            .where(
                Item.steam_price_usd.isnot(None),
                Item.price_usd.isnot(None),
                Item.price_usd <= max_price,
                Item.last_seen_at >= cutoff,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def upsert(self, **kwargs: Any) -> Item:
        """Создать или обновить предмет (add → flush, без commit)."""
        market_hash_name = kwargs["market_hash_name"]
        existing = await self.get_by_name(market_hash_name)
        now = datetime.now(timezone.utc)

        if existing:
            for key, value in kwargs.items():
                if value is not None:
                    setattr(existing, key, value)
            existing.last_seen_at = now
            await self._session.flush()
            return existing

        kwargs.setdefault("first_seen_at", now)
        kwargs.setdefault("last_seen_at", now)
        item = Item(**kwargs)
        self._session.add(item)
        await self._session.flush()
        return item

    async def upsert_sale_listing(
        self,
        c5_id: str,
        market_hash_name: str,
        item_name: str,
        price_usd: float,
        **kwargs: Any,
    ) -> SaleListing:
        """Создать или обновить ордер на продажу."""
        result = await self._session.execute(
            select(SaleListing).where(SaleListing.c5_id == c5_id)
        )
        listing = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)

        if listing is None:
            listing = SaleListing(
                c5_id=c5_id,
                market_hash_name=market_hash_name,
                item_name=item_name,
                price_usd=price_usd,
                is_active=True,
                scanned_at=now,
                **kwargs,
            )
            self._session.add(listing)
        else:
            listing.price_usd = price_usd
            listing.is_active = True
            listing.scanned_at = now
            for key, value in kwargs.items():
                if value is not None:
                    setattr(listing, key, value)

        await self._session.flush()
        return listing

    async def deactivate_stale_listings(self, active_c5_ids: set[str]) -> int:
        """Пометить неактивными ордера, которых больше нет на рынке."""
        if not active_c5_ids:
            return 0

        result = await self._session.execute(
            update(SaleListing)
            .where(SaleListing.is_active == True)
            .where(SaleListing.c5_id.not_in(active_c5_ids))
            .values(is_active=False)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def get_active_listings(
        self,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> Sequence[SaleListing]:
        """Получить активные ордера, опционально фильтруя по цене."""
        q = select(SaleListing).where(SaleListing.is_active == True)
        if min_price is not None:
            q = q.where(SaleListing.price_usd >= min_price)
        if max_price is not None:
            q = q.where(SaleListing.price_usd <= max_price)
        q = q.order_by(SaleListing.price_usd)
        result = await self._session.execute(q)
        return result.scalars().all()
