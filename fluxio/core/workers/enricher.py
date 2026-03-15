"""Enricher воркер — обогащение предметов данными pricehistory (30д медиана).

Алгоритм одного цикла:
1. SPOP steam:enrich_queue → один предмет для обогащения
2. Проверить enriched_at в БД — если свежий (< 7 дней), пропустить
3. Запросить Steam get_price_history() (требует steamLoginSecure)
4. Вычислить 30-дневную медиану через _calc_from_history()
5. Сохранить историю в steam_sales_history
6. Обновить Item: steam_median_30d, steam_volume_7d, enriched_at

Интервал между запросами — enricher_interval_seconds (по умолчанию 30с).
Обогащение происходит по одному предмету за цикл, чтобы не перегружать
Steam API (pricehistory требует авторизацию, без прокси).
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import datetime, timedelta, timezone

from loguru import logger

from fluxio.api.steam_client import SteamClient
from fluxio.config import config
from fluxio.core.workers.base import BaseWorker
from fluxio.db.models import SteamSalesHistory
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.utils.redis_client import KEY_ENRICH_QUEUE, get_redis

# Пауза если очередь пуста
_EMPTY_QUEUE_SLEEP = 60


class EnricherWorker(BaseWorker):
    """Обогащает предметы 30-дневной историей продаж Steam.

    Берёт предметы из Redis-очереди steam:enrich_queue,
    запрашивает pricehistory и сохраняет медиану в PostgreSQL.
    """

    def __init__(self, steam_client: SteamClient) -> None:
        interval = config.update_queue.enricher_interval_seconds
        super().__init__("enricher", interval_seconds=interval)
        self._steam = steam_client

    async def run_cycle(self) -> None:
        """Один цикл: взять предмет из очереди → обогатить."""
        redis = await get_redis()

        # SPOP: случайный элемент из очереди
        hash_name = await redis.spop(KEY_ENRICH_QUEUE)
        if not hash_name:
            logger.debug("Enricher: очередь пуста, ожидание...")
            await asyncio.sleep(_EMPTY_QUEUE_SLEEP)
            return

        try:
            await self._enrich_item(hash_name)
            self._status.items_processed += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Enricher: ошибка обогащения [{hash_name}]: {e}")

    async def _enrich_item(self, hash_name: str) -> None:
        """Обогатить один предмет данными pricehistory.

        1. Проверить свежесть enriched_at в БД
        2. Запросить pricehistory у Steam
        3. Сохранить историю продаж в steam_sales_history
        4. Обновить поля Item: steam_median_30d, steam_volume_7d, enriched_at
        """
        freshness_days = config.update_queue.enricher_freshness_days

        # Проверяем свежесть обогащения в БД
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item is None:
                logger.debug(f"Enricher: предмет [{hash_name}] не найден в БД")
                return

            if item.enriched_at is not None:
                age = datetime.now(timezone.utc) - item.enriched_at
                if age < timedelta(days=freshness_days):
                    logger.debug(
                        f"Enricher: [{hash_name}] уже обогащён "
                        f"({age.days}д назад < {freshness_days}д), пропуск"
                    )
                    return

        # Запрашиваем pricehistory у Steam
        app_id = self._get_app_id()
        history = await self._steam.get_price_history(hash_name, app_id=app_id)

        if history is None:
            logger.warning(
                f"Enricher: не удалось получить pricehistory [{hash_name}] "
                "(куки протухли?)"
            )
            return

        # Вычисляем 30-дневную медиану
        price_data = self._steam._calc_from_history(history, days=30)
        if price_data is None:
            logger.debug(f"Enricher: нет данных за 30 дней для [{hash_name}]")
            return

        # Вычисляем 7-дневный объём продаж
        volume_7d_data = self._steam._calc_from_history(history, days=7)
        volume_7d = volume_7d_data.sales_count if volume_7d_data else 0

        now = datetime.now(timezone.utc)

        # Сохраняем историю продаж в steam_sales_history
        await self._save_sales_history(hash_name, history)

        # Вычисляем метрики анти-манипуляции
        price_cv = self._calc_price_cv(history)
        spike_ratio = self._calc_spike_ratio(history)

        # Обновляем Item в БД
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item is not None:
                item.steam_median_30d = price_data.median_price_usd
                item.steam_volume_7d = volume_7d
                item.enriched_at = now
                item.price_stability_cv = price_cv
                item.price_spike_ratio = spike_ratio

                # sales_at_current_price считаем по текущей цене из Steam
                current_price = float(item.steam_price_usd or 0)
                if current_price > 0:
                    item.sales_at_current_price = self._calc_sales_at_price(
                        history, current_price,
                    )

                await uow.commit()

        logger.info(
            f"Enricher: обогащён [{hash_name}] "
            f"медиана 30д=${price_data.median_price_usd:.4f}, "
            f"объём 7д={volume_7d}, "
            f"CV={price_cv:.3f}, spike={spike_ratio:.2f}, "
            f"sales@price={item.sales_at_current_price if item else '?'}"
        )

    async def _save_sales_history(
        self, hash_name: str, history: list[list],
    ) -> None:
        """Сохранить историю продаж в таблицу steam_sales_history.

        Использует upsert по (hash_name, sale_date) — обновляет если запись есть.
        """
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)

        rows: list[dict] = []
        for entry in history:
            if len(entry) < 3:
                continue
            date_str, price, volume_str = entry[0], entry[1], entry[2]

            try:
                clean = date_str.split(": +")[0].strip()
                dt = datetime.strptime(clean, "%b %d %Y %H")  # noqa: DTZ007
            except (ValueError, IndexError):
                continue
            dt = dt.replace(tzinfo=timezone.utc)

            if dt < cutoff:
                continue

            vol = (
                int(volume_str.replace(",", ""))
                if isinstance(volume_str, str)
                else int(volume_str)
            )
            rows.append({
                "hash_name": hash_name,
                "sale_date": dt.strftime("%Y-%m-%d %H:%M"),
                "price_usd": float(price),
                "volume": vol,
            })

        if not rows:
            return

        async with UnitOfWork(async_session_factory) as uow:
            # Upsert: обновить цену и объём если запись уже есть
            stmt = pg_insert(SteamSalesHistory).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_steam_sales_hash_date",
                set_={
                    "price_usd": stmt.excluded.price_usd,
                    "volume": stmt.excluded.volume,
                },
            )
            await uow._session.execute(stmt)
            await uow.commit()

        logger.debug(
            f"Enricher: сохранено {len(rows)} записей истории [{hash_name}]"
        )

    @staticmethod
    def _calc_price_cv(history: list[list]) -> float:
        """Коэффициент вариации цены: stddev / median.

        Высокий CV = нестабильная цена (возможная манипуляция).
        """
        prices: list[float] = []
        for entry in history:
            if len(entry) >= 2:
                try:
                    prices.append(float(entry[1]))
                except (ValueError, TypeError):
                    continue

        if len(prices) < 2:
            return 0.0

        median = statistics.median(prices)
        if median <= 0:
            return 0.0

        stdev = statistics.stdev(prices)
        return stdev / median

    @staticmethod
    def _calc_spike_ratio(history: list[list]) -> float:
        """Отношение средней цены за последние 3 дня к средней за предыдущие 27 дней.

        Высокий ratio = подозрительный спайк цены.
        """
        now = datetime.now(timezone.utc)
        cutoff_3d = now - timedelta(days=3)
        cutoff_30d = now - timedelta(days=30)

        prices_3d: list[float] = []
        prices_27d: list[float] = []

        for entry in history:
            if len(entry) < 2:
                continue
            try:
                clean = entry[0].split(": +")[0].strip()
                dt = datetime.strptime(clean, "%b %d %Y %H")  # noqa: DTZ007
                dt = dt.replace(tzinfo=timezone.utc)
                price = float(entry[1])
            except (ValueError, TypeError, IndexError):
                continue

            if dt >= cutoff_3d:
                prices_3d.append(price)
            elif dt >= cutoff_30d:
                prices_27d.append(price)

        if not prices_3d or not prices_27d:
            return 1.0  # Нет данных — считаем нормальным

        avg_3d = sum(prices_3d) / len(prices_3d)
        avg_27d = sum(prices_27d) / len(prices_27d)

        if avg_27d <= 0:
            return 1.0

        return avg_3d / avg_27d

    @staticmethod
    def _calc_sales_at_price(
        history: list[list], current_price: float,
    ) -> int:
        """Кол-во продаж по цене в диапазоне ±20% от текущей.

        Мало продаж по этой цене = цена может быть искусственной.
        """
        low = current_price * 0.8
        high = current_price * 1.2
        total_volume = 0

        for entry in history:
            if len(entry) < 3:
                continue
            try:
                price = float(entry[1])
                vol_raw = entry[2]
                vol = (
                    int(vol_raw.replace(",", ""))
                    if isinstance(vol_raw, str)
                    else int(vol_raw)
                )
            except (ValueError, TypeError):
                continue

            if low <= price <= high:
                total_volume += vol

        return total_volume

    def _get_app_id(self) -> int:
        """Получить app_id первой включённой игры."""
        for game in config.games:
            if game.enabled:
                return game.app_id
        return 570  # Dota 2 по умолчанию
