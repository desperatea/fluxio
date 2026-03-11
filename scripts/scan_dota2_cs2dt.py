"""Сканирование всех предметов Dota 2 на CS2DT -> сохранение в PostgreSQL.

Обходит все страницы поиска CS2DT (appId=570), определяет тип покупки
(обычная/быстрая) и сохраняет каждый предмет в таблицу items.
"""

import asyncio
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger

# Настройка логов до импорта bot-модулей
logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db_helper import engine, async_session, init_db
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.db.models import Item

# ──────────────────────────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────────────────────────
PAGE_SIZE = 50
APP_ID = 570  # Dota 2


def determine_buy_type(price_info: dict) -> str:
    """Определить тип покупки по priceInfo."""
    has_normal = price_info.get("price") is not None and float(price_info.get("price", 0)) > 0
    has_auto = price_info.get("autoDeliverPrice") is not None and float(price_info.get("autoDeliverPrice", 0)) > 0

    if has_normal and has_auto:
        return "both"
    elif has_auto:
        return "quick"
    else:
        return "normal"


async def upsert_item(session: AsyncSession, **kwargs: dict) -> None:
    """Создать или обновить предмет в БД."""
    market_hash_name = kwargs["market_hash_name"]
    result = await session.execute(
        select(Item).where(Item.market_hash_name == market_hash_name)
    )
    item = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if item is None:
        item = Item(
            market_hash_name=market_hash_name,
            item_name=kwargs.get("item_name", ""),
            app_id=kwargs.get("app_id", APP_ID),
            cs2dt_item_id=kwargs.get("cs2dt_item_id"),
            image_url=kwargs.get("image_url"),
            price_usd=kwargs.get("price_usd"),
            auto_deliver_price_usd=kwargs.get("auto_deliver_price_usd"),
            quantity=kwargs.get("quantity", 0),
            auto_deliver_quantity=kwargs.get("auto_deliver_quantity", 0),
            buy_type=kwargs.get("buy_type", "normal"),
            listings_count=kwargs.get("listings_count", 0),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(item)
    else:
        item.item_name = kwargs.get("item_name", item.item_name)
        item.last_seen_at = now
        if kwargs.get("cs2dt_item_id") is not None:
            item.cs2dt_item_id = kwargs["cs2dt_item_id"]
        if kwargs.get("price_usd") is not None:
            item.price_usd = kwargs["price_usd"]
        if kwargs.get("auto_deliver_price_usd") is not None:
            item.auto_deliver_price_usd = kwargs["auto_deliver_price_usd"]
        item.quantity = kwargs.get("quantity", 0)
        item.auto_deliver_quantity = kwargs.get("auto_deliver_quantity", 0)
        item.buy_type = kwargs.get("buy_type", "normal")
        item.listings_count = kwargs.get("listings_count", 0)
        if kwargs.get("image_url"):
            item.image_url = kwargs["image_url"]


async def scan_all_dota2_items(client: CS2DTClient) -> int:
    """Обойти все страницы CS2DT Dota 2 и сохранить предметы в БД.

    Returns:
        Количество сохранённых предметов.
    """
    # 1. Первый запрос — узнаём total и pages
    logger.info("Запрос первой страницы CS2DT Dota 2...")
    first_page = await client.search_market(
        app_id=APP_ID, page=1, limit=PAGE_SIZE, order_by=1,
    )
    total = first_page.get("total", 0)
    total_pages = first_page.get("pages", 1)
    logger.info(f"Всего предметов Dota 2 на CS2DT: {total}, страниц: {total_pages}")

    if total == 0:
        logger.warning("Предметов не найдено!")
        return 0

    # 2. Собираем все предметы со всех страниц
    all_items: list[dict] = []

    # Обработать первую страницу
    items_list = first_page.get("list", [])
    all_items.extend(items_list)
    logger.info(f"Страница 1/{total_pages}: {len(items_list)} предметов")

    # Обойти оставшиеся страницы
    for page in range(2, total_pages + 1):
        try:
            data = await client.search_market(
                app_id=APP_ID, page=page, limit=PAGE_SIZE, order_by=1,
            )
            items_list = data.get("list", [])
            all_items.extend(items_list)

            if page % 10 == 0 or page == total_pages:
                logger.info(f"Страница {page}/{total_pages}: собрано {len(all_items)} предметов")

        except CS2DTAPIError as e:
            logger.error(f"Ошибка на странице {page}: {e}")
            continue

    logger.info(f"Собрано предметов с CS2DT: {len(all_items)}")

    # 3. Сохраняем в PostgreSQL
    saved = 0
    async with async_session() as session:
        for item_data in all_items:
            price_info = item_data.get("priceInfo", {})
            item_id = item_data.get("itemId")
            item_name = item_data.get("itemName", "")
            market_hash_name = item_data.get("marketHashName", "")

            if not market_hash_name:
                continue

            # Цены
            price = price_info.get("price")
            price_usd = float(price) if price else None
            auto_price = price_info.get("autoDeliverPrice")
            auto_deliver_price_usd = float(auto_price) if auto_price else None

            # Количество (могут быть None)
            quantity = int(price_info.get("quantity") or 0)
            auto_qty = int(price_info.get("autoDeliverQuantity") or 0)

            # Тип покупки
            buy_type = determine_buy_type(price_info)

            # Изображение
            image_url = item_data.get("imageUrl")

            await upsert_item(
                session,
                market_hash_name=market_hash_name,
                item_name=item_name,
                app_id=APP_ID,
                cs2dt_item_id=int(item_id) if item_id else None,
                image_url=image_url,
                price_usd=price_usd,
                auto_deliver_price_usd=auto_deliver_price_usd,
                quantity=quantity,
                auto_deliver_quantity=auto_qty,
                buy_type=buy_type,
                listings_count=quantity + auto_qty,
            )
            saved += 1

        await session.commit()

    logger.info(f"Сохранено в БД: {saved} предметов")
    return saved


async def main() -> None:
    """Точка входа скрипта."""
    logger.info("=== Сканирование Dota 2 предметов на CS2DT ===")

    # Инициализация PostgreSQL
    await init_db()

    client = CS2DTClient()
    try:
        # Проверяем баланс (заодно проверяем что API ключ работает)
        balance = await client.get_balance()
        logger.info(f"Баланс CS2DT: ${balance.get('data', '?')}")

        # Сканируем
        total_saved = await scan_all_dota2_items(client)

        # Статистика
        logger.info("=== Итого ===")
        logger.info(f"Предметов Dota 2 сохранено в БД: {total_saved}")

        # Показать статистику по типам покупки
        async with async_session() as session:
            for bt in ("normal", "quick", "both"):
                result = await session.execute(
                    select(func.count()).select_from(Item).where(Item.buy_type == bt)
                )
                count = result.scalar_one()
                logger.info(f"  {bt}: {count}")

            # Топ-5 самых дешёвых с автодоставкой
            result = await session.execute(
                select(Item)
                .where(Item.auto_deliver_price_usd.isnot(None))
                .where(Item.auto_deliver_price_usd > 0)
                .order_by(Item.auto_deliver_price_usd)
                .limit(5)
            )
            cheapest = result.scalars().all()
            if cheapest:
                logger.info("Топ-5 дешёвых с автодоставкой:")
                for it in cheapest:
                    logger.info(
                        f"  ${it.auto_deliver_price_usd:.2f} | "
                        f"{it.buy_type} | {it.item_name}"
                    )

    except CS2DTAPIError as e:
        logger.error(f"Ошибка CS2DT API: {e}")
    except Exception as e:
        logger.exception(f"Неожиданная ошибка: {e}")
    finally:
        await client.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
