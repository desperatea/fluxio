"""Scanner воркер — обход рынка CS2DT → БД → Redis очередь обновлений.

Алгоритм одного цикла:
1. Для каждой включённой игры (config.games) обойти все страницы CS2DT market
2. Upsert предметов в таблицу items (через UoW)
3. Рассчитать приоритет обновления Steam-цены для каждого предмета
4. ZADD в steam:update_queue (Sorted Set)

Приоритеты (DB.md раздел 7.2):
  100 — нет данных Steam вообще + предмет в ценовом диапазоне
  80  — был кандидатом / данные Steam старше freshness_candidate_minutes
  50  — в ценовом диапазоне + данные старше freshness_normal_hours
  20  — данные старше freshness_low_hours
  0   — пропустить (в blacklist / свежие данные)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
from loguru import logger

from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.config import config
from fluxio.core.workers.base import BaseWorker
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.utils.redis_client import (
    KEY_CANDIDATES,
    KEY_FRESHNESS,
    KEY_UPDATE_QUEUE,
    UpdatePriority,
    get_redis,
)


@dataclass
class ScanResult:
    """Итог одного цикла сканирования."""
    pages_fetched: int = 0
    items_upserted: int = 0
    queue_entries_added: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    app_ids_scanned: list[int] = field(default_factory=list)


class ScannerWorker(BaseWorker):
    """Сканирует CS2DT market → upsert items → Redis update_queue.

    Запускается как asyncio task через asyncio.create_task(scanner.run()).
    """

    def __init__(self, cs2dt_client: CS2DTClient) -> None:
        interval = config.update_queue.scanner_interval_seconds
        super().__init__("scanner", interval)
        self._cs2dt = cs2dt_client
        self._last_result: ScanResult | None = None

    @property
    def last_result(self) -> ScanResult | None:
        return self._last_result

    async def run_cycle(self) -> None:
        """Один полный скан CS2DT рынка."""
        started = datetime.now(timezone.utc)
        result = ScanResult()

        redis = await get_redis()
        blacklist = set(config.blacklist.items)

        # Сканируем все включённые игры
        for game in config.games:
            if not game.enabled:
                continue
            app_id = game.app_id
            logger.info(f"Scanner: скан {game.name} (appId={app_id})")

            try:
                page_result = await self._scan_game(app_id, redis, blacklist, result)
                result.app_ids_scanned.append(app_id)
                logger.info(
                    f"Scanner: {game.name} завершён — "
                    f"страниц: {page_result['pages']}, "
                    f"предметов: {page_result['items']}, "
                    f"в очереди: {page_result['queued']}"
                )
            except Exception as e:
                result.errors += 1
                logger.error(f"Scanner: ошибка скана {game.name}: {e}")

        result.duration_seconds = (
            datetime.now(timezone.utc) - started
        ).total_seconds()
        self._last_result = result
        self._status.items_processed += result.items_upserted

        logger.info(
            f"Scanner: цикл завершён — "
            f"предметов={result.items_upserted}, "
            f"в очереди={result.queue_entries_added}, "
            f"ошибок={result.errors}, "
            f"время={result.duration_seconds:.1f}с"
        )

    async def _scan_game(
        self,
        app_id: int,
        redis: aioredis.Redis,
        blacklist: set[str],
        result: ScanResult,
    ) -> dict[str, int]:
        """Скан одной игры: все страницы → DB → Redis."""
        all_items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1

        # Фазы 1: собрать все страницы
        while page <= total_pages:
            try:
                data = await self._cs2dt.search_market(
                    app_id=app_id,
                    page=page,
                    limit=50,
                    order_by=0,
                )
            except CS2DTAPIError as e:
                logger.warning(f"Scanner: CS2DT ошибка стр.{page}: {e}")
                result.errors += 1
                break
            except Exception as e:
                logger.error(f"Scanner: неожиданная ошибка стр.{page}: {e}")
                result.errors += 1
                break

            if not isinstance(data, dict):
                logger.warning(f"Scanner: неожиданный ответ стр.{page}")
                break

            items_page = data.get("list") or []
            all_items.extend(items_page)

            total_pages = int(data.get("pages", 1))
            result.pages_fetched += 1

            logger.debug(
                f"Scanner: appId={app_id} стр.{page}/{total_pages}, "
                f"предметов на странице: {len(items_page)}"
            )

            if page >= total_pages:
                break
            page += 1

            # Небольшая пауза между страницами (rate limit)
            await asyncio.sleep(0.2)

        if not all_items:
            return {"pages": result.pages_fetched, "items": 0, "queued": 0}

        # Фаза 2: upsert в БД и сбор данных для очереди
        items_upserted, queue_batch = await self._save_and_queue(
            all_items, app_id, redis, blacklist
        )
        result.items_upserted += items_upserted

        # Фаза 3: ZADD в Redis (батчами для эффективности)
        queued = 0
        if queue_batch:
            mapping = {name: score for name, score in queue_batch}
            # ZADD обновляет score если уже есть — берём максимум (GT флаг)
            # Используем GT чтобы не понижать приоритет
            await redis.zadd(KEY_UPDATE_QUEUE, mapping, gt=True)
            queued = len(mapping)
            result.queue_entries_added += queued

        return {"pages": result.pages_fetched, "items": items_upserted, "queued": queued}

    async def _save_and_queue(
        self,
        raw_items: list[dict[str, Any]],
        app_id: int,
        redis: aioredis.Redis,
        blacklist: set[str],
    ) -> tuple[int, list[tuple[str, float]]]:
        """Upsert предметов в БД и рассчитать приоритеты для очереди.

        Returns:
            (кол-во сохранённых предметов, список [(hash_name, priority)])
        """
        # Агрегируем по market_hash_name: берём минимальную цену
        items_map: dict[str, dict[str, Any]] = {}
        for raw in raw_items:
            hash_name = raw.get("marketHashName", "").strip()
            if not hash_name:
                continue

            price_info = raw.get("priceInfo") or {}
            # Цена в USD — поле price в priceInfo
            price_str = price_info.get("price") or raw.get("price") or "0"
            try:
                price_usd = float(str(price_str).replace(",", ""))
            except (ValueError, TypeError):
                price_usd = 0.0

            item_id = raw.get("itemId")
            if item_id is not None:
                try:
                    item_id = int(item_id)
                except (ValueError, TypeError):
                    item_id = None

            if hash_name not in items_map:
                items_map[hash_name] = {
                    "item_name": raw.get("itemName") or hash_name,
                    "app_id": app_id,
                    "cs2dt_item_id": item_id,
                    "min_price_usd": price_usd,
                    "listings_count": 1,
                    "price_usd": price_usd,
                }
            else:
                items_map[hash_name]["listings_count"] += 1
                if price_usd > 0 and (
                    items_map[hash_name]["min_price_usd"] == 0
                    or price_usd < items_map[hash_name]["min_price_usd"]
                ):
                    items_map[hash_name]["min_price_usd"] = price_usd
                    items_map[hash_name]["price_usd"] = price_usd

        items_saved = 0

        # Сохраняем в БД через UoW батчами по 200
        batch_size = 200
        names = list(items_map.keys())

        for batch_start in range(0, len(names), batch_size):
            batch_names = names[batch_start:batch_start + batch_size]
            async with UnitOfWork(async_session_factory) as uow:
                for name in batch_names:
                    info = items_map[name]
                    await uow.items.upsert(
                        market_hash_name=name,
                        item_name=info["item_name"],
                        app_id=info["app_id"],
                        cs2dt_item_id=info.get("cs2dt_item_id"),
                        price_usd=info["price_usd"] if info["price_usd"] > 0 else None,
                        min_price_usd_market=info["min_price_usd"] if info["min_price_usd"] > 0 else None,
                        listings_count=info["listings_count"],
                    )
                    items_saved += 1
                await uow.commit()

        # Рассчитываем приоритеты обновления Steam-цен
        queue_batch: list[tuple[str, float]] = []
        cfg_q = config.update_queue
        now = datetime.now(timezone.utc)

        # Получаем данные о свежести из Redis
        freshness_data = await redis.hgetall(KEY_FRESHNESS)
        candidates = await redis.smembers(KEY_CANDIDATES)

        min_price = config.trading.min_price_usd
        max_price = config.trading.max_price_usd

        for name, info in items_map.items():
            # Пропускаем blacklist
            if name in blacklist:
                continue

            price = info["min_price_usd"]
            in_range = min_price <= price <= max_price if price > 0 else False

            # Определяем свежесть Steam данных
            freshness_str = freshness_data.get(name)
            age_minutes: float | None = None
            if freshness_str:
                try:
                    last_updated = datetime.fromisoformat(freshness_str)
                    if last_updated.tzinfo is None:
                        last_updated = last_updated.replace(tzinfo=timezone.utc)
                    age_minutes = (now - last_updated).total_seconds() / 60
                except (ValueError, TypeError):
                    age_minutes = None

            # Рассчитываем приоритет по DB.md 7.2
            if age_minutes is None:
                # Нет данных Steam вообще
                priority = UpdatePriority.URGENT if in_range else UpdatePriority.MEDIUM
            elif name in candidates:
                # Был кандидатом — высокий приоритет
                priority = UpdatePriority.HIGH
            elif age_minutes < cfg_q.freshness_candidate_minutes:
                # Данные свежие — пропускаем
                priority = UpdatePriority.SKIP
            elif in_range and age_minutes >= cfg_q.freshness_candidate_minutes:
                # В диапазоне, данные устарели
                priority = UpdatePriority.HIGH if age_minutes >= cfg_q.freshness_normal_hours * 60 else UpdatePriority.MEDIUM
            elif age_minutes >= cfg_q.freshness_low_hours * 60:
                # Данные очень старые
                priority = UpdatePriority.LOW
            else:
                priority = UpdatePriority.SKIP

            if priority > UpdatePriority.SKIP:
                queue_batch.append((name, float(priority)))

        return items_saved, queue_batch
