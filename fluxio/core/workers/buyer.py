"""Buyer воркер — анализ кандидатов и покупка через CS2DT.

Алгоритм одного цикла:
1. SMEMBERS arb:candidates → получить список кандидатов
2. Для каждого кандидата:
   a. Загрузить данные из БД (Item + Steam цена)
   b. Redis SISMEMBER buy:purchased_ids → проверка идемпотентности
   c. DiscountStrategy.analyze() → решение о покупке
   d. safety.run_all_checks() → проверки безопасности
   e. dry_run: полная симуляция (баланс, прибыль) без CS2DT buy() | live: CS2DT buy()
   f. Сохранить Purchase в БД
   g. SADD buy:purchased_ids → пометить как куплено
   h. Pub/Sub → channel:purchases
3. Обработанные кандидаты → SREM arb:candidates

Скорость: buyer_interval_seconds (по умолчанию 60с).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

from fluxio.api.cs2dt_client import CS2DTClient
from fluxio.config import config
from fluxio.core.safety import SafetyCheck, run_all_checks
from fluxio.core.strategies.discount import DiscountStrategy
from fluxio.core.workers.base import BaseWorker
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.interfaces.market_client import MarketItem
from fluxio.interfaces.price_provider import PriceData
from fluxio.utils.redis_client import (
    CHANNEL_PURCHASES,
    KEY_CANDIDATES,
    KEY_PURCHASED_IDS,
    KEY_STEAM_PRICE,
    get_redis,
)

# Максимум кандидатов за один цикл (чтобы не перегружать)
MAX_CANDIDATES_PER_CYCLE = 10


class BuyerWorker(BaseWorker):
    """Покупает предметы из arb:candidates после проверок безопасности.

    Запускается как asyncio task через asyncio.create_task(buyer.run()).
    """

    def __init__(self, cs2dt_client: CS2DTClient) -> None:
        interval = config.update_queue.buyer_interval_seconds
        super().__init__("buyer", interval_seconds=interval)
        self._cs2dt = cs2dt_client
        self._strategy = DiscountStrategy(config)
        self._last_cycle_bought: int = 0
        self._last_cycle_skipped: int = 0
        self._total_bought: int = 0
        self._total_spent_usd: float = 0.0

    async def run_cycle(self) -> None:
        """Один цикл: забрать кандидатов → проверить → купить."""
        redis = await get_redis()

        # Получаем кандидатов из Redis
        candidate_names = list(await redis.smembers(KEY_CANDIDATES))
        if not candidate_names:
            logger.debug("Buyer: кандидатов нет, ожидание...")
            return

        # Ограничиваем количество за один цикл
        batch = candidate_names[:MAX_CANDIDATES_PER_CYCLE]
        logger.info(f"Buyer: обрабатываю {len(batch)} кандидатов из {len(candidate_names)}")

        bought = 0
        skipped = 0

        for hash_name in batch:
            try:
                result = await self._process_candidate(hash_name, redis)
                if result:
                    bought += 1
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

    async def _process_candidate(self, hash_name: str, redis: object) -> bool:
        """Обработать одного кандидата. Возвращает True если покупка состоялась."""
        # Загружаем данные предмета из БД
        async with UnitOfWork(async_session_factory) as uow:
            item = await uow.items.get_by_name(hash_name)
            if item is None:
                logger.debug(f"Buyer: [{hash_name}] не найден в БД, удаляю из кандидатов")
                await redis.srem(KEY_CANDIDATES, hash_name)
                return False

            item_price = float(item.price_usd or 0)
            steam_price = float(item.steam_price_usd or 0)

            if item_price <= 0 or steam_price <= 0:
                logger.debug(f"Buyer: [{hash_name}] нет цены, пропуск")
                await redis.srem(KEY_CANDIDATES, hash_name)
                return False

            # Получаем product_id из CS2DT (item_id хранится в БД)
            product_id = str(item.item_id) if hasattr(item, "item_id") and item.item_id else None
            if not product_id:
                logger.debug(f"Buyer: [{hash_name}] нет item_id, пропуск")
                return False

            # Redis: быстрая проверка идемпотентности
            already_bought = await redis.sismember(KEY_PURCHASED_IDS, product_id)
            if already_bought:
                logger.debug(f"Buyer: [{hash_name}] уже куплен (Redis), удаляю из кандидатов")
                await redis.srem(KEY_CANDIDATES, hash_name)
                return False

            # Стратегия: анализ
            market_item = MarketItem(
                item_id=product_id,
                product_id=product_id,
                market_hash_name=hash_name,
                item_name=item.item_name or hash_name,
                price=Decimal(str(item_price)),
                platform="cs2dt",
                app_id=item.app_id or 570,
            )

            # Получаем volume из Redis кэша или БД
            volume_24h = item.steam_volume_24h
            sales_7d = volume_24h * 7 if volume_24h else None

            reference = PriceData(
                market_hash_name=hash_name,
                median_price_usd=steam_price,
                lowest_price_usd=None,
                volume_24h=volume_24h,
                buy_order_price_usd=None,
                sales_7d=sales_7d,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

            analysis = self._strategy.analyze(market_item, reference)
            if not analysis.should_buy:
                logger.debug(f"Buyer: [{hash_name}] стратегия отклонила: {analysis.reason}")
                # Удаляем из кандидатов — Updater добавит обратно если условия изменятся
                await redis.srem(KEY_CANDIDATES, hash_name)
                return False

            # Safety checks (используем raw session для legacy safety.py)
            safety_result = await run_all_checks(
                cs2dt_client=self._cs2dt,
                session=uow._session,
                product_id=product_id,
                market_hash_name=hash_name,
                price_usd=item_price,
            )
            if not safety_result:
                logger.info(
                    f"Buyer: [{hash_name}] safety check не пройден: {safety_result.reason}"
                )
                return False

            # === ПОКУПКА ===
            if config.trading.dry_run:
                return await self._dry_run_buy(
                    uow, redis, hash_name, product_id,
                    item_price, steam_price, analysis.discount_percent,
                )
            else:
                return await self._live_buy(
                    uow, redis, hash_name, product_id,
                    item_price, steam_price, analysis.discount_percent,
                )

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
        """Симуляция покупки — полный цикл как live, кроме CS2DT buy().

        Проверяет баланс через API, trade_url, считает прибыль,
        создаёт Purchase (pending → success), генерирует out_trade_no.
        """
        out_trade_no = str(uuid.uuid4())
        trade_url = config.env.trade_url

        if not trade_url:
            logger.error("Buyer [DRY RUN]: TRADE_URL не задан в .env, покупка невозможна")
            return False

        # Проверяем баланс через API (как в реальном режиме)
        try:
            balance_data = await self._cs2dt.get_balance()
            balance = float(balance_data.get("data", 0))
            if balance < price_usd:
                logger.warning(
                    f"Buyer [DRY RUN]: [{hash_name}] недостаточно средств: "
                    f"баланс=${balance:.2f}, нужно=${price_usd:.4f}"
                )
                return False
            logger.debug(f"Buyer [DRY RUN]: баланс=${balance:.2f}")
        except Exception as e:
            logger.warning(f"Buyer [DRY RUN]: ошибка проверки баланса: {e}")
            # В dry-run продолжаем даже без баланса

        # Считаем ожидаемую прибыль
        fee_pct = config.fees.steam_fee_percent / 100
        net_steam = steam_price_usd * (1 - fee_pct)
        expected_profit = net_steam - price_usd

        # Создаём Purchase со статусом pending (как live)
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

        # Симулируем успешный ответ CS2DT (без реального вызова)
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

        # Помечаем как куплено в Redis
        await redis.sadd(KEY_PURCHASED_IDS, product_id)
        await redis.srem(KEY_CANDIDATES, hash_name)

        # Pub/Sub уведомление
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

        # Создаём запись покупки со статусом pending
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

            # Успешная покупка
            order_id = str(result.get("orderId", ""))
            buy_price = float(result.get("buyPrice", price_usd))
            delivery = result.get("delivery", 0)

            purchase.status = "success"
            purchase.order_id = order_id
            purchase.price_usd = buy_price
            purchase.api_response = result

            # Сохраняем активный заказ для трекинга
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

        # Помечаем как куплено
        await redis.sadd(KEY_PURCHASED_IDS, product_id)
        await redis.srem(KEY_CANDIDATES, hash_name)

        # Pub/Sub уведомление
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
