"""Enricher воркер — обогащение предметов данными pricehistory (30д медиана).

Алгоритм одного цикла:
1. SPOP steam:enrich_queue → N предметов (по числу auth-каналов)
2. Для каждого параллельно (asyncio.gather):
   a. Проверить enriched_at в БД — если свежий (< 7 дней), пропустить
   b. Запросить Steam get_price_history() (требует steamLoginSecure)
   c. Вычислить 30-дневную медиану через _calc_from_history()
   d. Сохранить историю в steam_sales_history
   e. Обновить Item: steam_median_30d, steam_volume_7d, enriched_at + метрики

Интервал между циклами — enricher_interval_seconds (по умолчанию 5с).
Параллельность — по числу доступных auth-каналов Steam.
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

    @property
    def _batch_size(self) -> int:
        """Размер батча = кол-во живых auth-каналов (минимум 1)."""
        live = sum(1 for ch in self._steam._auth_channels if ch.has_auth)
        return max(live, 1)

    async def run_cycle(self) -> None:
        """Один цикл: взять батч из очереди → обогатить параллельно."""
        redis = await get_redis()
        batch_size = self._batch_size

        # SPOP: забрать batch_size элементов из очереди
        names: list[str] = []
        for _ in range(batch_size):
            name = await redis.spop(KEY_ENRICH_QUEUE)
            if not name:
                break
            names.append(name)

        if not names:
            logger.debug("Enricher: очередь пуста, ожидание...")
            await asyncio.sleep(_EMPTY_QUEUE_SLEEP)
            return

        async def _safe_enrich(hash_name: str) -> bool:
            try:
                await self._enrich_item(hash_name)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Enricher: ошибка обогащения [{hash_name}]: {e}")
                return False

        results = await asyncio.gather(*[_safe_enrich(n) for n in names])
        ok_count = sum(1 for r in results if r)
        self._status.items_processed += ok_count

        if len(names) > 1:
            logger.info(f"Enricher: батч {ok_count}/{len(names)} обогащён")

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

        # Получаем item_nameid (однократно, кэшируется навсегда)
        item_nameid = await self._ensure_item_nameid(hash_name, app_id)

        # Обновляем Item в БД
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item is not None:
                item.steam_median_30d = price_data.median_price_usd
                item.steam_volume_7d = volume_7d
                item.enriched_at = now
                item.price_stability_cv = price_cv
                item.price_spike_ratio = spike_ratio
                if item_nameid is not None:
                    item.steam_item_nameid = item_nameid

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
            f"sales@price={item.sales_at_current_price if item else '?'}, "
            f"nameid={item_nameid or '?'}"
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
    def _parse_history_30d(history: list[list]) -> list[tuple[datetime, float, int]]:
        """Распарсить историю, отфильтровав только последние 30 дней.

        Возвращает список (datetime, price, volume).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        result: list[tuple[datetime, float, int]] = []

        for entry in history:
            if len(entry) < 3:
                continue
            try:
                clean = entry[0].split(": +")[0].strip()
                dt = datetime.strptime(clean, "%b %d %Y %H")  # noqa: DTZ007
                dt = dt.replace(tzinfo=timezone.utc)
                price = float(entry[1])
                vol_raw = entry[2]
                vol = (
                    int(vol_raw.replace(",", ""))
                    if isinstance(vol_raw, str)
                    else int(vol_raw)
                )
            except (ValueError, TypeError, IndexError):
                continue

            if dt >= cutoff:
                result.append((dt, price, vol))

        return result

    @staticmethod
    def _calc_price_cv(history: list[list]) -> float:
        """Коэффициент вариации цены: stddev / median (за 30 дней).

        Цены взвешиваются по volume. Выбросы (>3x от медианы) отбрасываются.
        Высокий CV = нестабильная цена.
        """
        parsed = EnricherWorker._parse_history_30d(history)
        if not parsed:
            return 0.0

        # Развернуть по volume: каждая продажа учитывается отдельно
        prices: list[float] = []
        for _dt, price, vol in parsed:
            prices.extend([price] * vol)

        if len(prices) < 2:
            return 0.0

        # Первый проход: медиана для определения выбросов
        median = statistics.median(prices)
        if median <= 0:
            return 0.0

        # Отбросить выбросы: цены > 5x от медианы или < 1/5 от медианы
        filtered = [p for p in prices if median / 5 <= p <= median * 5]
        if len(filtered) < 2:
            return 0.0

        median_clean = statistics.median(filtered)
        if median_clean <= 0:
            return 0.0

        stdev = statistics.stdev(filtered)
        return stdev / median_clean

    @staticmethod
    def _calc_spike_ratio(history: list[list]) -> float:
        """Отношение средневзвешенной цены за 3 дня к средневзвешенной за 27 дней.

        Взвешивание по объёму продаж — одна продажа по $7 не перевесит
        100 продаж по $0.40. Высокий ratio = подозрительный спайк цены.
        """
        cutoff_3d = datetime.now(timezone.utc) - timedelta(days=3)
        parsed = EnricherWorker._parse_history_30d(history)

        sum_pv_3d = 0.0
        sum_v_3d = 0
        sum_pv_27d = 0.0
        sum_v_27d = 0

        for dt, price, vol in parsed:
            effective_vol = max(vol, 1)
            if dt >= cutoff_3d:
                sum_pv_3d += price * effective_vol
                sum_v_3d += effective_vol
            else:
                sum_pv_27d += price * effective_vol
                sum_v_27d += effective_vol

        if sum_v_3d == 0 or sum_v_27d == 0:
            return 1.0  # Нет данных — считаем нормальным

        avg_3d = sum_pv_3d / sum_v_3d
        avg_27d = sum_pv_27d / sum_v_27d

        if avg_27d <= 0:
            return 1.0

        return avg_3d / avg_27d

    @staticmethod
    def _calc_sales_at_price(
        history: list[list], current_price: float,
    ) -> int:
        """Кол-во продаж по цене в диапазоне ±20% от текущей (за 30 дней).

        Мало продаж по этой цене = цена может быть искусственной.
        """
        low = current_price * 0.8
        high = current_price * 1.2
        total_volume = 0

        parsed = EnricherWorker._parse_history_30d(history)
        for _dt, price, vol in parsed:
            if low <= price <= high:
                total_volume += vol

        return total_volume

    async def _ensure_item_nameid(
        self, hash_name: str, app_id: int,
    ) -> int | None:
        """Получить item_nameid: из БД (кэш) или с парсинга страницы Steam.

        item_nameid не меняется, поэтому достаточно получить один раз.
        """
        # Проверяем кэш в БД
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item and item.steam_item_nameid:
                return item.steam_item_nameid

        # Запрашиваем с Steam (парсинг HTML страницы листинга)
        try:
            nameid = await self._steam.get_item_nameid(hash_name, app_id=app_id)
        except Exception as e:
            logger.debug(f"Enricher: ошибка получения item_nameid [{hash_name}]: {e}")
            return None

        if nameid:
            logger.debug(f"Enricher: получен item_nameid [{hash_name}] = {nameid}")

        return nameid

    def _get_app_id(self) -> int:
        """Получить app_id первой включённой игры."""
        for game in config.games:
            if game.enabled:
                return game.app_id
        return 570  # Dota 2 по умолчанию
