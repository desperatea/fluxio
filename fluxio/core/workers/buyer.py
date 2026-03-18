"""Buyer воркер — анализ кандидатов и покупка через CS2DT.

Алгоритм одного цикла:
1. SMEMBERS arb:candidates → получить список кандидатов
2. get_prices_batch(hash_names) → актуальные цены CS2DT (1 API-запрос)
3. Отфильтровать по наличию и цене
4. Для каждого выжившего — проверить в БД (steam_price, cs2dt_item_id)
5. Рассчитать buy_quantity по ликвидности
6. get_sell_list(cs2dt_item_id) → актуальные лоты
7. Для каждого лота — safety checks → покупка

Скорость: buyer_interval_seconds (по умолчанию 60с).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

from fluxio.api.cs2dt_client import CS2DTClient
from fluxio.config import FeesConfig, config
from fluxio.core.safety import SafetyCheck, run_all_checks
from fluxio.core.workers.base import BaseWorker
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.utils.redis_client import (
    CHANNEL_PURCHASES,
    KEY_CANDIDATES,
    KEY_PURCHASED_IDS,
    get_redis,
)

# Максимум кандидатов за один цикл
MAX_CANDIDATES_PER_CYCLE = 200  # лимит get_prices_batch


class BuyerWorker(BaseWorker):
    """Покупает предметы из arb:candidates после проверок безопасности."""

    def __init__(self, cs2dt_client: CS2DTClient) -> None:
        interval = config.update_queue.buyer_interval_seconds
        super().__init__("buyer", interval_seconds=interval)
        self._cs2dt = cs2dt_client
        self._last_cycle_bought: int = 0
        self._last_cycle_skipped: int = 0
        self._total_bought: int = 0
        self._total_spent_usd: float = 0.0

    async def run_cycle(self) -> None:
        """Один цикл: кандидаты → CS2DT цены → БД → покупка."""
        redis = await get_redis()

        # 1. Получаем кандидатов из Redis
        raw_names = await redis.smembers(KEY_CANDIDATES)
        candidate_names = [
            n if isinstance(n, str) else n.decode("utf-8") for n in raw_names
        ]
        if not candidate_names:
            logger.debug("Buyer: кандидатов нет, ожидание...")
            return

        batch = candidate_names[:MAX_CANDIDATES_PER_CYCLE]
        logger.info(f"Buyer: {len(batch)} кандидатов, запрашиваю цены CS2DT...")

        # 2. Batch-запрос актуальных цен CS2DT (1 API-вызов)
        try:
            cs2dt_prices = await self._cs2dt.get_prices_batch(batch, app_id=self._get_app_id())
        except Exception as e:
            logger.error(f"Buyer: ошибка get_prices_batch: {e}")
            return

        if not cs2dt_prices or not isinstance(cs2dt_prices, list):
            logger.debug("Buyer: CS2DT вернул пустые цены")
            return

        # Индексируем по hash_name
        cs2dt_by_name: dict[str, dict[str, Any]] = {}
        for item_data in cs2dt_prices:
            name = item_data.get("marketHashName", "")
            if name:
                cs2dt_by_name[name] = item_data

        # 3. Фильтруем: есть на CS2DT, цена в диапазоне, quantity > 0
        viable: list[tuple[str, float, int]] = []  # (hash_name, cs2dt_price, cs2dt_qty)
        for hash_name in batch:
            cs2dt_data = cs2dt_by_name.get(hash_name)
            if not cs2dt_data:
                # Нет на CS2DT — убираем из кандидатов
                await redis.srem(KEY_CANDIDATES, hash_name)
                continue

            price = float(cs2dt_data.get("price") or 0)
            qty = int(cs2dt_data.get("quantity") or 0)

            if qty <= 0 or price <= 0:
                await redis.srem(KEY_CANDIDATES, hash_name)
                continue

            if not (config.trading.min_price_usd <= price <= config.trading.max_price_usd):
                continue

            viable.append((hash_name, price, qty))

        if not viable:
            logger.debug("Buyer: нет предметов с подходящей ценой на CS2DT")
            return

        logger.info(f"Buyer: {len(viable)} предметов доступны на CS2DT, проверяю БД...")

        # 4. Для каждого — проверяем БД и покупаем
        bought = 0
        skipped = 0

        for hash_name, cs2dt_price, cs2dt_qty in viable:
            try:
                count = await self._process_item(
                    hash_name, cs2dt_price, cs2dt_qty, redis,
                )
                if count > 0:
                    bought += count
                else:
                    skipped += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Buyer: ошибка обработки [{hash_name}]: {e}")
                skipped += 1

        self._last_cycle_bought = bought
        self._last_cycle_skipped = skipped
        self._status.items_processed += bought
        logger.info(f"Buyer: цикл завершён — куплено: {bought}, пропущено: {skipped}")

    async def _process_item(
        self,
        hash_name: str,
        cs2dt_price: float,
        cs2dt_qty: int,
        redis: object,
    ) -> int:
        """Обработать предмет. Возвращает количество купленных штук."""
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)

            if item is None:
                # Нет в БД — нет steam_price, не можем оценить дисконт
                logger.debug(f"Buyer: [{hash_name}] нет в БД, пропуск")
                await redis.srem(KEY_CANDIDATES, hash_name)
                return 0

            steam_price = float(item.steam_price_usd or 0)
            cs2dt_item_id = item.cs2dt_item_id
            volume_24h = item.steam_volume_24h or 0

            if steam_price <= 0:
                logger.debug(f"Buyer: [{hash_name}] нет Steam цены, пропуск")
                await redis.srem(KEY_CANDIDATES, hash_name)
                return 0

            # Проверяем ROI по актуальной цене CS2DT
            net_steam = FeesConfig.calc_net_steam(
                steam_price,
                config.fees.steam_valve_fee_percent,
                config.fees.steam_game_fee_percent,
            )
            discount = (net_steam - cs2dt_price) / cs2dt_price * 100 if cs2dt_price > 0 else 0

            if discount < config.trading.min_discount_percent:
                logger.debug(
                    f"Buyer: [{hash_name}] дисконт {discount:.1f}% < "
                    f"{config.trading.min_discount_percent}%, пропуск"
                )
                await redis.srem(KEY_CANDIDATES, hash_name)
                return 0

            # Safety net: если по медиане 30д сделка убыточна — отклонить
            steam_median_30d = float(item.steam_median_30d or 0)
            if steam_median_30d > 0:
                net_median = FeesConfig.calc_net_steam(
                    steam_median_30d,
                    config.fees.steam_valve_fee_percent,
                    config.fees.steam_game_fee_percent,
                )
                if net_median < cs2dt_price:
                    logger.info(
                        f"Buyer: [{hash_name}] safety net — убыток по медиане "
                        f"(cs2dt=${cs2dt_price:.4f}, "
                        f"net_median=${net_median:.4f})"
                    )
                    await redis.srem(KEY_CANDIDATES, hash_name)
                    return 0

            if not cs2dt_item_id:
                logger.debug(f"Buyer: [{hash_name}] нет cs2dt_item_id, пропуск")
                return 0

            # Рассчитываем количество для покупки
            buy_quantity = self._calc_buy_quantity(volume_24h)

            # Сколько уже купили за 24ч
            already_bought = await uow.purchases.get_same_item_count_24h(hash_name)
            remaining_allowed = max(0, config.trading.max_same_item_count - already_bought)
            if remaining_allowed == 0:
                logger.debug(
                    f"Buyer: [{hash_name}] лимит достигнут: "
                    f"{already_bought}/{config.trading.max_same_item_count} за 24ч"
                )
                await redis.srem(KEY_CANDIDATES, hash_name)
                return 0

            buy_quantity = min(buy_quantity, remaining_allowed, cs2dt_qty)

            # Получаем актуальные лоты с CS2DT (сортировка по цене ↑)
            try:
                sell_data = await self._cs2dt.get_sell_list(
                    item_id=cs2dt_item_id,
                    order_by=2,  # цена по возрастанию
                    limit=min(buy_quantity * 2, 50),
                    max_price=str(config.trading.max_price_usd),
                )
            except Exception as e:
                logger.error(f"Buyer: [{hash_name}] ошибка get_sell_list: {e}")
                return 0

            listings = sell_data.get("list", []) if sell_data else []
            if not listings:
                logger.debug(f"Buyer: [{hash_name}] нет лотов на CS2DT")
                await redis.srem(KEY_CANDIDATES, hash_name)
                return 0

            # Покупаем лоты последовательно
            total_bought = 0

            for listing in listings:
                if total_bought >= buy_quantity:
                    break

                product_id = str(listing.get("id", ""))
                listing_price = float(listing.get("price", 0))

                if not product_id or listing_price <= 0:
                    continue

                if listing_price < config.trading.min_price_usd:
                    continue
                if listing_price > config.trading.max_price_usd:
                    break  # отсортировано по цене, дальше дороже

                listing_discount = (
                    (net_steam - listing_price) / listing_price * 100 if listing_price > 0 else 0
                )
                if listing_discount < config.trading.min_discount_percent:
                    break  # дальше дисконт только меньше

                # Идемпотентность: Redis (быстро) + PostgreSQL (надёжно)
                already_in_redis = await redis.sismember(KEY_PURCHASED_IDS, product_id)
                if already_in_redis:
                    continue
                already_in_db = await uow.purchases.exists(product_id)
                if already_in_db:
                    # Восстанавливаем Redis из БД
                    await redis.sadd(KEY_PURCHASED_IDS, product_id)
                    continue

                # Safety checks
                safety_result = await run_all_checks(
                    cs2dt_client=self._cs2dt,
                    session=uow.session,
                    product_id=product_id,
                    market_hash_name=hash_name,
                    price_usd=listing_price,
                )
                if not safety_result:
                    if safety_result.fatal:
                        logger.warning(
                            f"Buyer: [{hash_name}] фатальная проверка не пройдена: "
                            f"{safety_result.reason}"
                        )
                        break  # баланс/лимит исчерпан — стоп всех покупок
                    else:
                        logger.debug(
                            f"Buyer: [{hash_name}] пропуск листинга: {safety_result.reason}"
                        )
                        continue  # пропускаем этот листинг, пробуем следующий

                # === ПОКУПКА ===
                if config.trading.dry_run:
                    ok = await self._dry_run_buy(
                        uow, redis, hash_name, product_id,
                        listing_price, steam_price, listing_discount,
                    )
                else:
                    ok = await self._live_buy(
                        uow, redis, hash_name, product_id,
                        listing_price, steam_price, listing_discount,
                    )

                if ok:
                    total_bought += 1

            # Удаляем из кандидатов — Updater добавит обратно при следующем цикле
            await redis.srem(KEY_CANDIDATES, hash_name)

            if total_bought > 0:
                logger.info(
                    f"Buyer: [{hash_name}] куплено {total_bought} шт "
                    f"(план: {buy_quantity}, лотов: {len(listings)})"
                )

            return total_bought

    @staticmethod
    def _calc_buy_quantity(daily_sales: int) -> int:
        """Рассчитать количество для покупки на основе ликвидности.

        Тиры по дневным продажам в Steam:
        - >= 50 продаж/день: до daily_sales штук (1 дневной объём)
        - >= 10 продаж/день: до daily_sales * 0.5 (полдня)
        - < 10 продаж/день: до min(3, daily_sales)
        - нет данных: 1 штука
        """
        if daily_sales <= 0:
            return 1
        if daily_sales >= 50:
            return max(1, int(daily_sales * 1.0))
        if daily_sales >= 10:
            return max(1, int(daily_sales * 0.5))
        return max(1, min(3, daily_sales))

    def _get_app_id(self) -> int:
        """Получить app_id первой включённой игры."""
        for game in config.games:
            if game.enabled:
                return game.app_id
        return 570

    async def _dry_run_buy(
        self,
        uow: UnitOfWork,
        redis: object,
        hash_name: str,
        product_id: str,
        price_usd: float,
        steam_price_usd: float,
        discount_percent: float,
    ) -> bool:
        """Симуляция покупки — полный цикл как live, кроме CS2DT buy()."""
        out_trade_no = str(uuid.uuid4())
        trade_url = config.env.trade_url

        if not trade_url:
            logger.error("Buyer [DRY RUN]: TRADE_URL не задан в .env, покупка невозможна")
            return False

        # Проверяем баланс через API
        try:
            balance_data = await self._cs2dt.get_balance()
            balance = float(balance_data.get("data", 0))
            if balance < price_usd:
                logger.warning(
                    f"Buyer [DRY RUN]: [{hash_name}] недостаточно средств: "
                    f"баланс=${balance:.2f}, нужно=${price_usd:.4f}"
                )
                return False
        except Exception as e:
            logger.warning(f"Buyer [DRY RUN]: ошибка проверки баланса: {e}")

        # Считаем ожидаемую прибыль
        net_steam = FeesConfig.calc_net_steam(
            steam_price_usd,
            config.fees.steam_valve_fee_percent,
            config.fees.steam_game_fee_percent,
        )
        expected_profit = net_steam - price_usd

        # Создаём Purchase
        purchase = await uow.purchases.create(
            product_id=product_id,
            market_hash_name=hash_name,
            price_usd=price_usd,
            steam_price_usd=steam_price_usd,
            discount_percent=discount_percent,
            status="pending",
            dry_run=True,
        )
        await uow.commit()

        simulated_order_id = f"DRY-{out_trade_no[:8]}"
        purchase.status = "success"
        purchase.order_id = simulated_order_id
        purchase.api_response = {
            "dry_run": True,
            "orderId": simulated_order_id,
            "buyPrice": price_usd,
            "delivery": 2,
            "out_trade_no": out_trade_no,
            "expected_profit_usd": round(expected_profit, 4),
        }
        await uow.commit()

        logger.info(
            f"Buyer [DRY RUN]: ПОКУПКА [{hash_name}] "
            f"цена=${price_usd:.4f}, steam=${steam_price_usd:.4f}, "
            f"дисконт={discount_percent:.1f}%, "
            f"прибыль=${expected_profit:.4f}, "
            f"ордер={simulated_order_id}"
        )

        await redis.sadd(KEY_PURCHASED_IDS, product_id)
        await self._publish_purchase(redis, hash_name, price_usd, discount_percent, dry_run=True)

        self._total_bought += 1
        self._total_spent_usd += price_usd
        return True

    async def _live_buy(
        self,
        uow: UnitOfWork,
        redis: object,
        hash_name: str,
        product_id: str,
        price_usd: float,
        steam_price_usd: float,
        discount_percent: float,
    ) -> bool:
        """Реальная покупка через CS2DT API."""
        out_trade_no = str(uuid.uuid4())
        trade_url = config.env.trade_url

        if not trade_url:
            logger.error("Buyer: TRADE_URL не задан в .env, покупка невозможна")
            return False

        purchase = await uow.purchases.create(
            product_id=product_id,
            market_hash_name=hash_name,
            price_usd=price_usd,
            steam_price_usd=steam_price_usd,
            discount_percent=discount_percent,
            status="pending",
            dry_run=False,
        )
        await uow.commit()

        try:
            result = await self._cs2dt.buy(
                product_id=product_id,
                out_trade_no=out_trade_no,
                trade_url=trade_url,
                max_price=price_usd,
            )

            order_id = str(result.get("orderId", ""))
            buy_price = float(result.get("buyPrice", price_usd))
            delivery = result.get("delivery", 0)

            purchase.status = "success"
            purchase.order_id = order_id
            purchase.price_usd = buy_price
            purchase.api_response = result

            if order_id:
                await uow.purchases.save_active_order(
                    order_id=order_id,
                    purchase_id=purchase.id,
                    status="pending",
                )

            await uow.commit()

            logger.info(
                f"Buyer: КУПЛЕНО [{hash_name}] "
                f"цена=${buy_price:.4f}, ордер={order_id}, "
                f"доставка={'авто' if delivery == 2 else 'P2P'}"
            )

        except Exception as e:
            purchase.status = "failed"
            purchase.api_response = {"error": str(e)}
            await uow.commit()
            logger.error(f"Buyer: ошибка покупки [{hash_name}]: {e}")
            return False

        await redis.sadd(KEY_PURCHASED_IDS, product_id)
        await self._publish_purchase(redis, hash_name, price_usd, discount_percent, dry_run=False)

        self._total_bought += 1
        self._total_spent_usd += price_usd
        return True

    async def _publish_purchase(
        self,
        redis: object,
        hash_name: str,
        price_usd: float,
        discount_percent: float,
        *,
        dry_run: bool,
    ) -> None:
        """Отправить уведомление о покупке в Redis Pub/Sub."""
        try:
            msg = json.dumps({
                "event": "purchase",
                "item": hash_name,
                "price_usd": price_usd,
                "discount_percent": discount_percent,
                "dry_run": dry_run,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await redis.publish(CHANNEL_PURCHASES, msg)
        except Exception as e:
            logger.debug(f"Buyer: ошибка pub/sub уведомления: {e}")

    def stats(self) -> dict:
        """Статистика покупок для дашборда."""
        return {
            "total_bought": self._total_bought,
            "total_spent_usd": round(self._total_spent_usd, 4),
            "last_cycle_bought": self._last_cycle_bought,
            "last_cycle_skipped": self._last_cycle_skipped,
        }
