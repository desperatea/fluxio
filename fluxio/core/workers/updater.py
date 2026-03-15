"""Updater воркер — обновление Steam-цен из Redis очереди.

Алгоритм одного цикла:
1. ZPOPMAX steam:update_queue → батч предметов с наивысшим приоритетом
2. Для каждого предмета (параллельно через asyncio.gather):
   a. Проверить кэш steam:price:{name} (TTL 30 мин) — если свежий, пропустить
   b. Запросить Steam get_price_overview() (быстро, без куки)
   c. Закэшировать в Redis с TTL 30 мин
   d. Обновить steam:freshness hash
   e. Обновить Item в БД (steam_price_usd, steam_volume_24h, steam_updated_at)
   f. Проверить enrichment из БД (steam_median_30d, enriched_at)
      — если нет обогащения → SADD steam:enrich_queue, пропустить
      — если есть → рассчитать дисконт, проверить стабильность цены
   g. Дисконт >= порога: SADD arb:candidates, иначе SREM
3. Если очередь пуста — пауза 30с и повтор

Размер батча и параллельность — динамические, зависят от кол-ва прокси.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger

from fluxio.api.steam_client import SteamClient
from fluxio.config import config
from fluxio.core.workers.base import BaseWorker
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.utils.redis_client import (
    KEY_CANDIDATES,
    KEY_ENRICH_QUEUE,
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

    Использует только priceoverview (быстро, без куки).
    30-дневная медиана берётся из БД (поля enrichment от EnricherWorker).
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

        1. Проверить кэш Redis → если нет, запросить priceoverview
        2. Обновить кэш, freshness, DB
        3. Проверить enrichment (30д медиана из БД)
           — нет enrichment → отправить в очередь обогащения
           — есть → проверить стабильность и дисконт → кандидаты
        """
        redis_client = redis  # type alias для clarity

        # ── Шаг 1: Получить текущую цену (priceoverview) ──
        cache_key = KEY_STEAM_PRICE.format(hash_name)
        cached = await redis_client.hgetall(cache_key)

        steam_lowest: float | None = None
        steam_median_24h: float | None = None
        steam_volume_24h: int | None = None

        if cached and cached.get("median"):
            try:
                steam_median_24h = float(cached["median"])
                steam_volume_24h = int(cached.get("volume", 0)) or None
                steam_lowest = float(cached["lowest"]) if cached.get("lowest") else None
                logger.debug(f"Updater: кэш [{hash_name}] lowest=${steam_lowest}, median_24h=${steam_median_24h:.4f}")
            except (ValueError, TypeError):
                cached = {}

        if steam_median_24h is None:
            # Запрашиваем только priceoverview (быстро, без куки)
            app_id = self._get_app_id()
            overview = await self._steam.get_price_overview(hash_name, app_id=app_id)
            overview_data = self._steam._parse_overview(overview) if overview else None

            if overview_data is None:
                logger.debug(f"Updater: Steam не вернул цену для [{hash_name}]")
                return

            steam_median_24h = overview_data.median_price_usd
            steam_volume_24h = overview_data.sales_count
            steam_lowest = overview_data.lowest_price_usd

            # Кэшируем в Redis (TTL 30 мин)
            cache_mapping: dict[str, str] = {
                "median": str(steam_median_24h),
            }
            if steam_volume_24h is not None:
                cache_mapping["volume"] = str(steam_volume_24h)
            if steam_lowest is not None:
                cache_mapping["lowest"] = str(steam_lowest)

            await redis_client.hset(cache_key, mapping=cache_mapping)
            await redis_client.expire(cache_key, STEAM_PRICE_TTL)

        # Текущая цена листинга для расчёта дисконта
        steam_current = steam_lowest if steam_lowest and steam_lowest > 0 else steam_median_24h

        # ── Шаг 2: Обновить freshness и БД ──
        now_iso = datetime.now(timezone.utc).isoformat()
        await redis_client.hset(KEY_FRESHNESS, hash_name, now_iso)

        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item is None:
                logger.debug(f"Updater: предмет [{hash_name}] не найден в БД")
                return

            item.steam_price_usd = steam_current
            if steam_volume_24h is not None:
                item.steam_volume_24h = steam_volume_24h
            item.steam_updated_at = datetime.now(timezone.utc)
            await uow.commit()

            # ── Шаг 3: Проверить enrichment и решить по кандидатам ──
            item_price = float(item.price_usd or 0)
            if item_price <= 0 or steam_current <= 0:
                await redis_client.srem(KEY_CANDIDATES, hash_name)
                return

            # Проверяем наличие обогащения (30д медиана из БД)
            enrichment_fresh = self._is_enrichment_fresh(item)

            if not enrichment_fresh:
                # Нет обогащения или устарело → отправить в очередь enricher
                await redis_client.sadd(KEY_ENRICH_QUEUE, hash_name)
                await redis_client.srem(KEY_CANDIDATES, hash_name)
                logger.debug(
                    f"Updater: [{hash_name}] нет обогащения → "
                    f"отправлен в очередь enricher"
                )
                return

            # Есть свежее обогащение — используем 30д медиану из БД
            steam_median_30d = float(item.steam_median_30d)
            steam_volume_7d = item.steam_volume_7d or 0

            fee = config.fees.steam_fee_percent / 100
            net_steam = steam_current * (1 - fee)
            discount = (net_steam - item_price) / net_steam * 100

            # Проверяем ликвидность (объём продаж за 7 дней)
            volume_ok = steam_volume_7d >= config.trading.min_sales_volume_7d

            # Проверка анти-манипуляции (данные из enricher)
            am = config.anti_manipulation
            spike_ratio = item.price_spike_ratio
            price_cv = item.price_stability_cv
            sales_at_price = item.sales_at_current_price

            spike_ok = (spike_ratio or 0) <= am.max_spike_ratio
            cv_ok = (price_cv or 0) <= am.max_price_cv
            sales_ok = (sales_at_price or 0) >= am.min_sales_at_current_price
            manipulation_ok = spike_ok and cv_ok and sales_ok

            if not manipulation_ok:
                reasons: list[str] = []
                if not spike_ok:
                    reasons.append(
                        f"spike={spike_ratio:.2f}>{am.max_spike_ratio}"
                    )
                if not cv_ok:
                    reasons.append(
                        f"CV={price_cv:.3f}>{am.max_price_cv}"
                    )
                if not sales_ok:
                    reasons.append(
                        f"sales@price={sales_at_price}<{am.min_sales_at_current_price}"
                    )
                logger.info(
                    f"Updater: [{hash_name}] отклонён — "
                    f"анти-манипуляция: {', '.join(reasons)}"
                )

            if (
                discount >= config.trading.min_discount_percent
                and volume_ok
                and manipulation_ok
                and config.trading.min_price_usd <= item_price <= config.trading.max_price_usd
            ):
                await redis_client.sadd(KEY_CANDIDATES, hash_name)
                logger.info(
                    f"Updater: КАНДИДАТ [{hash_name}] "
                    f"дисконт={discount:.1f}%, "
                    f"цена=${item_price:.4f}, "
                    f"steam=${steam_current:.4f}, "
                    f"медиана 30д=${steam_median_30d:.4f}, "
                    f"CV={price_cv:.3f}, spike={spike_ratio:.2f}, "
                    f"sales@price={sales_at_price}"
                )
            else:
                await redis_client.srem(KEY_CANDIDATES, hash_name)

        logger.debug(
            f"Updater: обновлён [{hash_name}] "
            f"steam=${steam_current:.4f}"
        )

    @staticmethod
    def _is_enrichment_fresh(item: object) -> bool:
        """Проверить, есть ли свежее обогащение у предмета."""
        if item.steam_median_30d is None or item.enriched_at is None:
            return False
        age = datetime.now(timezone.utc) - item.enriched_at
        freshness_days = config.update_queue.enricher_freshness_days
        return age < timedelta(days=freshness_days)

    def _get_app_id(self) -> int:
        """Получить app_id первой включённой игры."""
        for game in config.games:
            if game.enabled:
                return game.app_id
        return 570  # Dota 2 по умолчанию
