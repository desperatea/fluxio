"""Сканер рынка C5Game → БД.

C5Game API использует внутренние ID категорий (1–1000+) вместо Steam appId.
Каждая категория = тип предмета для конкретного героя Dota 2.
Для получения ВСЕХ предметов нужно обойти все категории с пагинацией.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from fluxio.api.c5game_client import C5GameClient, C5GameAPIError
from fluxio.config import config
from fluxio.db.repository import (
    async_session,
    deactivate_stale_listings,
    upsert_item,
    upsert_sale_listing,
)

# Диапазон категорий C5Game для Dota 2
# Верифицировано 2026-03-08: категории 1–1000, ~487 непустых, ~13300 лотов
MAX_CATEGORY_ID = 1000
PAGE_SIZE = 50


class MarketScanner:
    """Сканирует рынок C5Game Dota 2 и сохраняет подходящие ордера в БД."""

    def __init__(self, c5_client: C5GameClient) -> None:
        self._c5 = c5_client

    async def scan(self) -> ScanResult:
        """Полный скан: обход всех категорий → фильтр → запись в БД."""
        min_price = config.trading.min_price_cny
        max_price = config.trading.max_price_cny

        logger.info(
            f"Скан C5Game Dota 2 — категории 1–{MAX_CATEGORY_ID}, "
            f"фильтр цены: {min_price}–{max_price} CNY"
        )

        # 1. Обойти все категории
        all_listings = await self._fetch_all_categories()
        logger.info(f"Всего лотов на C5Game Dota 2: {len(all_listings)}")

        if not all_listings:
            return ScanResult(
                total=0, filtered=0, items_saved=0,
                listings_saved=0, categories_scanned=0,
            )

        # 2. Фильтр по цене
        filtered = [
            lst for lst in all_listings
            if min_price <= lst.get("price", 0) <= max_price
        ]
        logger.info(
            f"После фильтра по цене ({min_price}–{max_price} CNY): "
            f"{len(filtered)} лотов"
        )

        # 3. Сохранить в БД
        items_saved, listings_saved = await self._save_to_db(filtered, all_listings)

        result = ScanResult(
            total=len(all_listings),
            filtered=len(filtered),
            items_saved=items_saved,
            listings_saved=listings_saved,
            categories_scanned=MAX_CATEGORY_ID,
        )
        logger.info(
            f"Скан завершён: всего={result.total}, "
            f"подходящих={result.filtered}, "
            f"предметов={result.items_saved}, "
            f"ордеров={result.listings_saved}"
        )
        return result

    async def _fetch_all_categories(self) -> list[dict[str, Any]]:
        """Обойти все категории 1–MAX_CATEGORY_ID, собрать все лоты."""
        all_items: list[dict[str, Any]] = []
        categories_with_items = 0

        for cat_id in range(1, MAX_CATEGORY_ID + 1):
            items = await self._fetch_category(cat_id)
            if items:
                all_items.extend(items)
                categories_with_items += 1

            # Прогресс каждые 100 категорий
            if cat_id % 100 == 0:
                logger.info(
                    f"Прогресс: категорий {cat_id}/{MAX_CATEGORY_ID}, "
                    f"лотов собрано: {len(all_items)}, "
                    f"непустых категорий: {categories_with_items}"
                )

        logger.info(
            f"Обход завершён: {categories_with_items} непустых категорий "
            f"из {MAX_CATEGORY_ID}"
        )
        return all_items

    async def _fetch_category(self, category_id: int) -> list[dict[str, Any]]:
        """Получить все страницы одной категории."""
        items: list[dict[str, Any]] = []
        page = 1

        while True:
            try:
                data = await self._c5.search_market(
                    game_id=category_id,
                    page=page,
                    page_size=PAGE_SIZE,
                )
            except C5GameAPIError:
                break

            if not isinstance(data, dict):
                break

            listings = data.get("list") or []
            if not listings:
                break

            items.extend(listings)

            total = int(data.get("total", 0))
            pages = int(data.get("pages", 1))

            if page >= pages:
                break
            page += 1

        return items

    async def _save_to_db(
        self,
        filtered: list[dict[str, Any]],
        all_listings: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Сохранить предметы и ордера в БД.

        Args:
            filtered: лоты, подходящие по цене (сохраняются как ордера).
            all_listings: все лоты (для агрегации каталога предметов).

        Returns:
            (количество предметов, количество ордеров)
        """
        # Агрегируем уникальные предметы по market_hash_name
        items_map: dict[str, dict[str, Any]] = {}
        for lst in all_listings:
            hash_name = lst.get("marketHashName", "")
            if not hash_name:
                continue
            price = float(lst.get("price", 0))
            if hash_name not in items_map:
                items_map[hash_name] = {
                    "item_name": lst.get("itemName", ""),
                    "min_price": price,
                    "count": 1,
                    "image_url": lst.get("imageUrl"),
                    "hero": (lst.get("itemInfo") or {}).get("hero"),
                    "slot": (lst.get("itemInfo") or {}).get("slot"),
                    "rarity": (lst.get("itemInfo") or {}).get("rarityName"),
                    "quality": (lst.get("itemInfo") or {}).get("qualityName"),
                }
            else:
                items_map[hash_name]["count"] += 1
                if price < items_map[hash_name]["min_price"]:
                    items_map[hash_name]["min_price"] = price

        items_saved = 0
        listings_saved = 0

        async with async_session() as session:
            # Фаза 1: Сохраняем предметы (каталог)
            for hash_name, info in items_map.items():
                await upsert_item(
                    session,
                    market_hash_name=hash_name,
                    item_name=info["item_name"],
                    hero=info.get("hero"),
                    slot=info.get("slot"),
                    rarity=info.get("rarity"),
                    quality=info.get("quality"),
                    image_url=info.get("image_url"),
                    min_price_cny=info["min_price"],
                    listings_count=info["count"],
                )
                items_saved += 1
            await session.commit()
            logger.info(f"Сохранено предметов в каталог: {items_saved}")

            # Фаза 2: Сохраняем подходящие ордера на продажу
            active_c5_ids: set[str] = set()
            for lst in filtered:
                c5_id = str(lst.get("id", ""))
                if not c5_id:
                    continue
                active_c5_ids.add(c5_id)

                seller_info = lst.get("sellerInfo") or {}
                await upsert_sale_listing(
                    session,
                    c5_id=c5_id,
                    market_hash_name=lst.get("marketHashName", ""),
                    item_name=lst.get("itemName", ""),
                    price_cny=float(lst.get("price", 0)),
                    seller_id=seller_info.get("userId"),
                    delivery=int(lst.get("delivery", 0)),
                    accept_bargain=bool(lst.get("acceptBargain", 0)),
                    raw_data=lst,
                )
                listings_saved += 1

            # Деактивировать ордера, которых больше нет
            deactivated = await deactivate_stale_listings(session, active_c5_ids)
            if deactivated:
                logger.info(f"Деактивировано устаревших ордеров: {deactivated}")

            await session.commit()

        return items_saved, listings_saved


class ScanResult:
    """Результат скана рынка."""

    __slots__ = (
        "total", "filtered", "items_saved",
        "listings_saved", "categories_scanned",
    )

    def __init__(
        self,
        total: int,
        filtered: int,
        items_saved: int,
        listings_saved: int,
        categories_scanned: int = 0,
    ) -> None:
        self.total = total
        self.filtered = filtered
        self.items_saved = items_saved
        self.listings_saved = listings_saved
        self.categories_scanned = categories_scanned

    def __repr__(self) -> str:
        return (
            f"ScanResult(total={self.total}, filtered={self.filtered}, "
            f"items={self.items_saved}, listings={self.listings_saved}, "
            f"categories={self.categories_scanned})"
        )
