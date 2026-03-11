"""CRUD операции для базы данных."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from loguru import logger

from fluxio.config import config
from fluxio.db.models import (
    ActiveOrder,
    ApiLog,
    Base,
    Blacklist,
    ConfigVersion,
    DailyStat,
    Item,
    PriceHistory,
    Purchase,
    SaleListing,
    Watchlist,
)

# Async engine и session factory
engine = create_async_engine(
    config.env.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создать все таблицы (для разработки — в продакшне через Alembic)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База данных инициализирована")


# ──────────────────────────────────────────────────
# Покупки
# ──────────────────────────────────────────────────

async def save_purchase(
    session: AsyncSession,
    product_id: str,
    market_hash_name: str,
    price_cny: float,
    status: str = "pending",
    steam_price_usd: float | None = None,
    discount_percent: float | None = None,
    dry_run: bool = False,
    api_response: dict[str, Any] | None = None,
    order_id: str | None = None,
) -> Purchase:
    """Сохранить покупку в БД."""
    purchase = Purchase(
        product_id=product_id,
        order_id=order_id,
        market_hash_name=market_hash_name,
        price_cny=price_cny,
        steam_price_usd=steam_price_usd,
        discount_percent=discount_percent,
        status=status,
        dry_run=dry_run,
        api_response=api_response,
    )
    session.add(purchase)
    await session.commit()
    logger.info(f"Покупка сохранена: {market_hash_name} за {price_cny} CNY (статус: {status})")
    return purchase


async def get_today_spent(session: AsyncSession) -> float:
    """Получить сумму трат за сегодня."""
    today = date.today()
    result = await session.execute(
        select(func.coalesce(func.sum(Purchase.price_cny), 0))
        .where(Purchase.dry_run == False)
        .where(Purchase.status.in_(["pending", "success"]))
        .where(func.date(Purchase.purchased_at) == today)
    )
    return float(result.scalar_one())


async def get_same_item_count_24h(
    session: AsyncSession,
    market_hash_name: str,
) -> int:
    """Получить количество покупок одного предмета за 24 часа."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await session.execute(
        select(func.count())
        .select_from(Purchase)
        .where(Purchase.market_hash_name == market_hash_name)
        .where(Purchase.dry_run == False)
        .where(Purchase.status.in_(["pending", "success"]))
        .where(Purchase.purchased_at >= cutoff)
    )
    return result.scalar_one()


async def is_product_purchased(session: AsyncSession, product_id: str) -> bool:
    """Проверить, был ли product_id уже куплен."""
    result = await session.execute(
        select(func.count())
        .select_from(Purchase)
        .where(Purchase.product_id == product_id)
    )
    return result.scalar_one() > 0


# ──────────────────────────────────────────────────
# История цен
# ──────────────────────────────────────────────────

async def save_price_snapshot(
    session: AsyncSession,
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
    session.add(record)
    await session.commit()
    return record


async def cleanup_old_records(session: AsyncSession, days: int = 90) -> int:
    """Удалить записи price_history и api_logs старше N дней."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    result1 = await session.execute(
        delete(PriceHistory).where(PriceHistory.recorded_at < cutoff)
    )
    result2 = await session.execute(
        delete(ApiLog).where(ApiLog.logged_at < cutoff)
    )
    await session.commit()

    total = (result1.rowcount or 0) + (result2.rowcount or 0)
    if total > 0:
        logger.info(f"Удалено {total} старых записей (price_history + api_logs)")
    return total


# ──────────────────────────────────────────────────
# Активные заказы
# ──────────────────────────────────────────────────

async def save_active_order(
    session: AsyncSession,
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
    session.add(order)
    await session.commit()
    return order


async def get_stale_orders(
    session: AsyncSession,
    stale_minutes: int = 10,
) -> Sequence[ActiveOrder]:
    """Получить зависшие заказы (старше N минут)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    result = await session.execute(
        select(ActiveOrder)
        .where(ActiveOrder.status == "pending")
        .where(ActiveOrder.created_at < cutoff)
    )
    return result.scalars().all()


# ──────────────────────────────────────────────────
# Чёрный / белый списки
# ──────────────────────────────────────────────────

async def get_blacklist_names(session: AsyncSession) -> set[str]:
    """Получить множество заблокированных market_hash_name."""
    result = await session.execute(select(Blacklist.market_hash_name))
    return set(result.scalars().all())


async def add_to_blacklist(
    session: AsyncSession,
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
    session.add(item)
    await session.commit()
    logger.info(f"Добавлено в чёрный список: {market_hash_name}")
    return item


# ──────────────────────────────────────────────────
# Ежедневная статистика
# ──────────────────────────────────────────────────

async def update_daily_stats(
    session: AsyncSession,
    stat_date: date | None = None,
) -> DailyStat:
    """Обновить/создать ежедневную статистику."""
    stat_date = stat_date or date.today()

    result = await session.execute(
        select(DailyStat).where(DailyStat.date == stat_date)
    )
    stat = result.scalar_one_or_none()

    # Посчитать агрегаты из покупок
    purchases_result = await session.execute(
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
        session.add(stat)

    stat.items_purchased = row.count
    stat.total_spent_cny = float(row.spent)
    stat.potential_value_usd = float(row.value)
    stat.potential_profit_usd = float(row.value) - float(row.spent) / config.fees.usd_to_cny_rate

    await session.commit()
    return stat


# ──────────────────────────────────────────────────
# Предметы (каталог)
# ──────────────────────────────────────────────────

async def upsert_item(
    session: AsyncSession,
    market_hash_name: str,
    item_name: str,
    app_id: int = 570,
    cs2dt_item_id: int | None = None,
    hero: str | None = None,
    slot: str | None = None,
    rarity: str | None = None,
    quality: str | None = None,
    image_url: str | None = None,
    price_usd: float | None = None,
    auto_deliver_price_usd: float | None = None,
    quantity: int = 0,
    auto_deliver_quantity: int = 0,
    buy_type: str = "normal",
    min_price_cny: float | None = None,
    listings_count: int = 0,
    steam_price_usd: float | None = None,
    steam_volume_24h: int | None = None,
) -> Item:
    """Создать или обновить предмет в каталоге."""
    result = await session.execute(
        select(Item).where(Item.market_hash_name == market_hash_name)
    )
    item = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if item is None:
        item = Item(
            market_hash_name=market_hash_name,
            item_name=item_name,
            app_id=app_id,
            cs2dt_item_id=cs2dt_item_id,
            hero=hero,
            slot=slot,
            rarity=rarity,
            quality=quality,
            image_url=image_url,
            price_usd=price_usd,
            auto_deliver_price_usd=auto_deliver_price_usd,
            quantity=quantity,
            auto_deliver_quantity=auto_deliver_quantity,
            buy_type=buy_type,
            min_price_cny=min_price_cny,
            listings_count=listings_count,
            steam_price_usd=steam_price_usd,
            steam_volume_24h=steam_volume_24h,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(item)
    else:
        item.item_name = item_name
        item.last_seen_at = now
        if cs2dt_item_id is not None:
            item.cs2dt_item_id = cs2dt_item_id
        if price_usd is not None:
            item.price_usd = price_usd
        if auto_deliver_price_usd is not None:
            item.auto_deliver_price_usd = auto_deliver_price_usd
        item.quantity = quantity
        item.auto_deliver_quantity = auto_deliver_quantity
        item.buy_type = buy_type
        if min_price_cny is not None:
            item.min_price_cny = min_price_cny
        item.listings_count = listings_count
        if hero:
            item.hero = hero
        if slot:
            item.slot = slot
        if rarity:
            item.rarity = rarity
        if quality:
            item.quality = quality
        if image_url:
            item.image_url = image_url
        if steam_price_usd is not None:
            item.steam_price_usd = steam_price_usd
        if steam_volume_24h is not None:
            item.steam_volume_24h = steam_volume_24h

    return item


async def get_all_items(session: AsyncSession) -> Sequence[Item]:
    """Получить все предметы из каталога."""
    result = await session.execute(select(Item).order_by(Item.market_hash_name))
    return result.scalars().all()


# ──────────────────────────────────────────────────
# Ордера на продажу (C5Game listings)
# ──────────────────────────────────────────────────

async def upsert_sale_listing(
    session: AsyncSession,
    c5_id: str,
    market_hash_name: str,
    item_name: str,
    price_cny: float,
    seller_id: str | None = None,
    delivery: int = 0,
    accept_bargain: bool = False,
    raw_data: dict[str, Any] | None = None,
) -> SaleListing:
    """Создать или обновить ордер на продажу."""
    result = await session.execute(
        select(SaleListing).where(SaleListing.c5_id == c5_id)
    )
    listing = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if listing is None:
        listing = SaleListing(
            c5_id=c5_id,
            market_hash_name=market_hash_name,
            item_name=item_name,
            price_cny=price_cny,
            seller_id=seller_id,
            delivery=delivery,
            accept_bargain=accept_bargain,
            raw_data=raw_data,
            is_active=True,
            scanned_at=now,
        )
        session.add(listing)
    else:
        listing.price_cny = price_cny
        listing.is_active = True
        listing.scanned_at = now
        if raw_data is not None:
            listing.raw_data = raw_data

    return listing


async def deactivate_stale_listings(
    session: AsyncSession,
    active_c5_ids: set[str],
) -> int:
    """Пометить неактивными ордера, которых больше нет на рынке.

    Если active_c5_ids пустой — пропускаем (скан мог не вернуть результатов
    из-за ошибки; деактивация всех листингов была бы деструктивной).
    """
    if not active_c5_ids:
        logger.warning("deactivate_stale_listings: пустой active_c5_ids — пропускаю")
        return 0

    result = await session.execute(
        update(SaleListing)
        .where(SaleListing.is_active == True)
        .where(SaleListing.c5_id.not_in(active_c5_ids))
        .values(is_active=False)
    )
    deactivated = result.rowcount or 0
    if deactivated > 0:
        logger.debug(f"Деактивировано {deactivated} устаревших листингов")
    return deactivated


async def get_active_listings(
    session: AsyncSession,
    min_price: float | None = None,
    max_price: float | None = None,
) -> Sequence[SaleListing]:
    """Получить активные ордера, опционально фильтруя по цене."""
    q = select(SaleListing).where(SaleListing.is_active == True)
    if min_price is not None:
        q = q.where(SaleListing.price_cny >= min_price)
    if max_price is not None:
        q = q.where(SaleListing.price_cny <= max_price)
    q = q.order_by(SaleListing.price_cny)
    result = await session.execute(q)
    return result.scalars().all()
