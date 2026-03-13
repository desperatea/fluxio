"""Updater воркер — обновление Steam-цен из Redis очереди.

Алгоритм одного цикла:
1. ZPOPMAX steam:update_queue → батч предметов с наивысшим приоритетом
2. Для каждого предмета (параллельно через asyncio.gather):
   a. Проверить кэш steam:price:{name} (TTL 30 мин) — если свежий, пропустить
   b. Запросить Steam get_median_price()
   c. Закэшировать в Redis с TTL 30 мин
   d. Обновить steam:freshness hash
   e. Обновить Item в БД (steam_price_usd, steam_volume_24h, steam_updated_at)
   f. Рассчитать дисконт — если >= порога: SADD arb:candidates
      иначе: SREM arb:candidates
3. Если очередь пуста — пауза 30с и повтор

Размер батча и параллельность — динамические, зависят от кол-ва прокси.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger

from fluxio.api.steam_client import SteamClient
from fluxio.config import config
from fluxio.core.workers.base import BaseWorker
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.utils.redis_client import (
    KEY_CANDIDATES,
    KEY_FRESHNESS,
    KEY_STEAM_PRICE,
    KEY_UPDATE_QUEUE,
    STEAM_PRICE_TTL,
    get_redis,
)

# Минимальный размер батча (если мало прокси)
_MIN_BATCH = 10
# Пауза если очередь пуста
EMPTY_QUEUE_SLEEP = 30


class UpdaterWorker(BaseWorker):
    """Обновляет Steam-цены из Redis-очереди.

    Запускается как asyncio task через asyncio.create_task(updater.run()).
    Размер батча динамический — равен кол-ву прокси-каналов.
    """

    def __init__(self, steam_client: SteamClient) -> None:
        # Интервал = 0, т.к. воркер сам управляет паузами внутри run_cycle
        super().__init__("updater", interval_seconds=0)
        self._steam = steam_client

    @property
    def _batch_size(self) -> int:
        """Динамический размер батча — по кол-ву доступных каналов."""
        channels = len(self._steam._proxy_urls)
        return max(channels, _MIN_BATCH)

    async def run_cycle(self) -> None:
        """Один цикл: вытащить батч из очереди → обновить Steam цены параллельно."""
        redis = await get_redis()
        batch_size = self._batch_size

        # ZPOPMAX: забрать batch_size элементов с наивысшим приоритетом
        raw_batch = await redis.zpopmax(KEY_UPDATE_QUEUE, count=batch_size)

        if not raw_batch:
            # Очередь пуста — пауза
            logger.debug("Updater: очередь пуста, ожидание...")
            await asyncio.sleep(EMPTY_QUEUE_SLEEP)
            return

        logger.info(f"Updater: обрабатываю {len(raw_batch)} предметов параллельно (каналов: {batch_size})")

        # Параллельная обработка всего батча
        async def _safe_update(hash_name: str) -> bool:
            """Обновить один предмет, перехватывая ошибки."""
            try:
                await self._update_item(hash_name, redis)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Updater: ошибка обновления [{hash_name}]: {e}")
                return False

        names = [
            member if isinstance(member, str) else member.decode("utf-8")
            for member, _score in raw_batch
        ]

        results = await asyncio.gather(*[_safe_update(n) for n in names])
        ok_count = sum(1 for r in results if r)
        self._status.items_processed += ok_count

        queue_len = await redis.zcard(KEY_UPDATE_QUEUE)
        logger.info(
            f"Updater: батч {ok_count}/{len(names)} обработан, "
            f"в очереди осталось: {queue_len}"
        )

    async def _update_item(self, hash_name: str, redis: object) -> None:  # type: ignore[type-arg]
        """Обновить Steam-цену одного предмета.

        1. Проверить кэш Redis
        2. Если нет — запросить Steam API
        3. Обновить кэш, freshness, DB, candidates
        """
        redis_client = redis  # type alias для clarity

        # Проверяем кэш Steam цены
        cache_key = KEY_STEAM_PRICE.format(hash_name)
        cached = await redis_client.hgetall(cache_key)

        steam_median: float | None = None
        steam_volume: int | None = None
        steam_lowest: float | None = None

        if cached and cached.get("median"):
            # Используем кэш
            try:
                steam_median = float(cached["median"])
                steam_volume = int(cached.get("volume", 0)) or None
                steam_lowest = float(cached["lowest"]) if cached.get("lowest") else None
                logger.debug(f"Updater: кэш [{hash_name}] median=${steam_median:.4f}")
            except (ValueError, TypeError):
                cached = {}  # сбрасываем битый кэш

        if steam_median is None:
            # Запрашиваем у Steam
            app_id = self._get_app_id()
            price_data = await self._steam.get_median_price(hash_name, app_id=app_id)

            if price_data is None:
                logger.debug(f"Updater: Steam не вернул цену для [{hash_name}]")
                return

            steam_median = float(price_data.median_price_usd)
            steam_volume = price_data.sales_count
            steam_lowest = float(price_data.lowest_price_usd) if price_data.lowest_price_usd else None

            # Кэшируем в Redis (TTL 30 мин)
            cache_mapping: dict[str, str] = {"median": str(steam_median)}
            if steam_volume is not None:
                cache_mapping["volume"] = str(steam_volume)
            if steam_lowest is not None:
                cache_mapping["lowest"] = str(steam_lowest)

            await redis_client.hset(cache_key, mapping=cache_mapping)
            await redis_client.expire(cache_key, STEAM_PRICE_TTL)

        # Обновляем freshness
        now_iso = datetime.now(timezone.utc).isoformat()
        await redis_client.hset(KEY_FRESHNESS, hash_name, now_iso)

        # Обновляем БД
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item is not None:
                item.steam_price_usd = steam_median
                if steam_volume is not None:
                    item.steam_volume_24h = steam_volume
                if hasattr(item, "steam_updated_at"):
                    item.steam_updated_at = datetime.now(timezone.utc)
                await uow.commit()

                # Рассчитываем дисконт и обновляем кандидатов
                item_price = float(item.price_usd or 0)
                if item_price > 0 and steam_median > 0:
                    fee = config.fees.steam_fee_percent / 100
                    net_steam = steam_median * (1 - fee)
                    discount = (net_steam - item_price) / net_steam * 100

                    # Проверяем ликвидность
                    volume_ok = (
                        steam_volume is not None
                        and steam_volume >= config.trading.min_sales_volume_7d
                    )

                    if (
                        discount >= config.trading.min_discount_percent
                        and volume_ok
                        and config.trading.min_price_usd <= item_price <= config.trading.max_price_usd
                    ):
                        await redis_client.sadd(KEY_CANDIDATES, hash_name)
                        logger.info(
                            f"Updater: КАНДИДАТ [{hash_name}] "
                            f"дисконт={discount:.1f}%, "
                            f"цена=${item_price:.4f}, "
                            f"steam=${steam_median:.4f}"
                        )
                    else:
                        await redis_client.srem(KEY_CANDIDATES, hash_name)
                else:
                    await redis_client.srem(KEY_CANDIDATES, hash_name)
            else:
                logger.debug(f"Updater: предмет [{hash_name}] не найден в БД")

        logger.debug(
            f"Updater: обновлён [{hash_name}] "
            f"steam=${steam_median:.4f}"
        )

    def _get_app_id(self) -> int:
        """Получить app_id первой включённой игры."""
        for game in config.games:
            if game.enabled:
                return game.app_id
        return 570  # Dota 2 по умолчанию
