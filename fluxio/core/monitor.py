"""Мониторинг рынка Dota 2 на C5Game.

Цикл мониторинга (SPEC.md раздел 7.1):
1. Получить все страницы рынка Dota 2 (пагинация по категориям)
2. Отфильтровать по ценовому диапазону
3. Для отфильтрованных — запросить эталонные цены Steam
4. Передать выгодные предметы в модуль покупки
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from fluxio.api.c5game_client import C5GameClient, C5GameAPIError
from fluxio.api.steam_client import SteamClient, SteamPriceData
from fluxio.config import config


# Категории Dota 2 на C5Game (верифицированы сканированием 1-100)
# Каждая категория = тип слота (neck, back, weapon и т.д.)
DOTA2_CATEGORIES = list(range(1, 101))


class MarketMonitor:
    """Мониторинг рынка — обход всех страниц, сбор данных, анализ."""

    def __init__(
        self,
        c5_client: C5GameClient,
        steam_client: SteamClient,
    ) -> None:
        self._c5 = c5_client
        self._steam = steam_client
        self._previous_product_ids: set[str] = set()

    async def run_cycle(self) -> list[MarketItem]:
        """Выполнить один цикл мониторинга.

        Returns:
            Список предметов в ценовом диапазоне с эталонными ценами Steam.
        """
        logger.info("Цикл мониторинга — начало")

        # 1. Собрать все лоты с C5Game
        all_items = await self._fetch_all_listings()
        logger.info(f"Получено лотов с C5Game: {len(all_items)}")

        if not all_items:
            logger.warning("Нет лотов на рынке — пропускаю цикл")
            return []

        # 2. Фильтр по ценовому диапазону
        min_price = config.trading.min_price_cny
        max_price = config.trading.max_price_cny
        filtered = [
            item for item in all_items
            if min_price <= item.price_cny <= max_price
        ]
        logger.info(
            f"После фильтра по цене ({min_price}–{max_price} CNY): "
            f"{len(filtered)} лотов"
        )

        if not filtered:
            logger.info("Нет предметов в ценовом диапазоне — пропускаю")
            return []

        # 3. Отслеживание новых листингов
        current_ids = {item.product_id for item in all_items}
        new_ids = current_ids - self._previous_product_ids
        if self._previous_product_ids:
            logger.info(f"Новых листингов: {len(new_ids)}")
        self._previous_product_ids = current_ids

        # 4. Получить эталонные цены Steam для уникальных hashName
        unique_names = list({item.market_hash_name for item in filtered})
        logger.info(
            f"Уникальных hashName для запроса Steam: {len(unique_names)}"
        )
        steam_prices = await self._fetch_steam_prices(unique_names)

        # 5. Привязать Steam цены к предметам
        for item in filtered:
            steam_data = steam_prices.get(item.market_hash_name)
            if steam_data:
                item.steam_price = steam_data

        items_with_steam = [
            item for item in filtered if item.steam_price is not None
        ]
        logger.info(
            f"Предметов с ценами Steam: {items_with_steam.__len__()} / {len(filtered)}"
        )

        logger.info(
            f"Цикл мониторинга завершён. "
            f"Всего: {len(all_items)}, "
            f"в диапазоне: {len(filtered)}, "
            f"с ценами Steam: {len(items_with_steam)}"
        )

        return items_with_steam

    async def _fetch_all_listings(self) -> list[MarketItem]:
        """Получить все лоты Dota 2 с C5Game по всем категориям."""
        all_items: list[MarketItem] = []

        for cat_id in DOTA2_CATEGORIES:
            try:
                items = await self._fetch_category(cat_id)
                all_items.extend(items)
            except C5GameAPIError as e:
                logger.debug(f"Категория {cat_id}: {e}")
                continue

        return all_items

    async def _fetch_category(self, category_id: int) -> list[MarketItem]:
        """Получить все страницы одной категории."""
        items: list[MarketItem] = []
        page = 1

        while True:
            try:
                data = await self._c5.search_market(
                    game_id=category_id,
                    page=page,
                    page_size=50,
                )
            except C5GameAPIError:
                break

            if not isinstance(data, dict):
                break

            listings = data.get("list") or data.get("records") or []
            if not listings:
                break

            for item_data in listings:
                item = self._parse_listing(item_data, category_id)
                if item:
                    items.append(item)

            # Проверяем есть ли следующая страница
            total_str = data.get("total", "0")
            try:
                total = int(total_str) if isinstance(total_str, str) else total_str
            except (ValueError, TypeError):
                total = 0

            if page * 50 >= total:
                break
            page += 1

        return items

    @staticmethod
    def _parse_listing(data: dict[str, Any], category_id: int) -> MarketItem | None:
        """Распарсить один лот из ответа C5Game."""
        try:
            product_id = str(data.get("id", ""))
            if not product_id:
                return None

            price = float(data.get("price", 0))
            if price <= 0:
                return None

            item_name = data.get("itemName") or data.get("name") or ""
            market_hash_name = data.get("marketHashName") or ""
            if not market_hash_name:
                return None

            delivery = int(data.get("delivery", 0))

            return MarketItem(
                product_id=product_id,
                item_name=item_name,
                market_hash_name=market_hash_name,
                price_cny=price,
                category_id=category_id,
                delivery=delivery,
                raw_data=data,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"Ошибка парсинга лота: {e}")
            return None

    async def _fetch_steam_prices(
        self,
        hash_names: list[str],
    ) -> dict[str, SteamPriceData]:
        """Получить эталонные цены Steam для списка hashName.

        Запросы идут последовательно из-за жёсткого rate limit Steam.
        """
        prices: dict[str, SteamPriceData] = {}
        days = config.monitoring.price_history_days

        for i, name in enumerate(hash_names):
            result = await self._steam.get_median_price(name, days=days)
            if result:
                prices[name] = result

            if (i + 1) % 50 == 0:
                logger.info(
                    f"Steam цены: {i + 1}/{len(hash_names)} обработано"
                )

        return prices


class MarketItem:
    """Предмет с рынка C5Game + опциональная цена Steam."""

    __slots__ = (
        "product_id",
        "item_name",
        "market_hash_name",
        "price_cny",
        "category_id",
        "delivery",
        "raw_data",
        "steam_price",
    )

    def __init__(
        self,
        product_id: str,
        item_name: str,
        market_hash_name: str,
        price_cny: float,
        category_id: int,
        delivery: int,
        raw_data: dict[str, Any],
    ) -> None:
        self.product_id = product_id
        self.item_name = item_name
        self.market_hash_name = market_hash_name
        self.price_cny = price_cny
        self.category_id = category_id
        self.delivery = delivery
        self.raw_data = raw_data
        self.steam_price: SteamPriceData | None = None

    def __repr__(self) -> str:
        steam = f", steam=${self.steam_price.median_price_usd:.4f}" if self.steam_price else ""
        return (
            f"MarketItem({self.market_hash_name!r}, "
            f"c5={self.price_cny} CNY{steam})"
        )
