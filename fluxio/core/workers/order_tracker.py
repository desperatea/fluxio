"""OrderTracker воркер — отслеживание доставки заказов.

Алгоритм одного цикла:
1. Получить все active_orders со статусом pending из БД
2. Для каждого: запросить CS2DT get_order_detail()
3. Обновить статус:
   - status 10 (Successful) → delivered, обновить delivered_at
   - status 11 (Fail) → failed
   - status 1-3 (в процессе) → оставить pending
4. Зависшие заказы (>12ч для P2P) → автоотмена

Интервал: 5 минут.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from fluxio.api.cs2dt_client import CS2DTClient
from fluxio.core.workers.base import BaseWorker
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork

# Интервал проверки заказов
ORDER_CHECK_INTERVAL = 300  # 5 минут

# Таймаут P2P доставки — автоотмена зависших заказов
P2P_STALE_HOURS = 12

# Маппинг статусов CS2DT → внутренние
CS2DT_STATUS_MAP = {
    1: "pending",       # WaitingDelivery
    2: "pending",       # Delivery
    3: "pending",       # WaitingAccept
    10: "delivered",    # Successful
    11: "failed",       # Fail
}


class OrderTrackerWorker(BaseWorker):
    """Отслеживает статусы заказов и автоотменяет зависшие.

    Запускается как asyncio task через asyncio.create_task(tracker.run()).
    """

    def __init__(self, cs2dt_client: CS2DTClient) -> None:
        super().__init__("order_tracker", interval_seconds=ORDER_CHECK_INTERVAL)
        self._cs2dt = cs2dt_client
        self._total_delivered: int = 0
        self._total_failed: int = 0
        self._total_cancelled: int = 0

    async def run_cycle(self) -> None:
        """Один цикл: проверить статусы активных заказов."""
        async with UnitOfWork(async_session_factory) as uow:
            # Получаем все pending-заказы
            stale_orders = await uow.purchases.get_stale_orders(stale_minutes=0)

            if not stale_orders:
                logger.debug("OrderTracker: активных заказов нет")
                return

            logger.info(f"OrderTracker: проверяю {len(stale_orders)} активных заказов")

            for order in stale_orders:
                try:
                    await self._check_order(uow, order)
                except Exception as e:
                    logger.error(
                        f"OrderTracker: ошибка проверки ордера {order.order_id}: {e}"
                    )

            await uow.commit()

        self._status.items_processed = (
            self._total_delivered + self._total_failed + self._total_cancelled
        )

    async def _check_order(self, uow: UnitOfWork, order: object) -> None:
        """Проверить статус одного заказа через CS2DT API."""
        order_id = order.order_id
        created_at = order.created_at

        # Проверяем таймаут P2P (>12ч)
        age = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)
        if age > timedelta(hours=P2P_STALE_HOURS):
            logger.warning(
                f"OrderTracker: ордер {order_id} зависший ({age.total_seconds() / 3600:.1f}ч), "
                f"пытаюсь отменить"
            )
            try:
                await self._cs2dt.cancel_order(order_id)
                order.status = "cancelled"
                self._total_cancelled += 1
                logger.info(f"OrderTracker: ордер {order_id} отменён (таймаут P2P)")

                # Обновляем Purchase
                if order.purchase_id:
                    from fluxio.db.models import Purchase
                    from sqlalchemy import select
                    result = await uow._session.execute(
                        select(Purchase).where(Purchase.id == order.purchase_id)
                    )
                    purchase = result.scalar_one_or_none()
                    if purchase:
                        purchase.status = "cancelled"
                        purchase.notes = f"Автоотмена: P2P таймаут {P2P_STALE_HOURS}ч"
            except Exception as e:
                logger.error(f"OrderTracker: ошибка отмены ордера {order_id}: {e}")
            return

        # Запрашиваем статус у CS2DT
        try:
            detail = await self._cs2dt.get_order_detail(order_id=order_id)
        except Exception as e:
            logger.debug(f"OrderTracker: не удалось получить статус {order_id}: {e}")
            # Обновляем last_checked_at
            order.last_checked_at = datetime.now(timezone.utc)
            return

        # Определяем новый статус
        cs2dt_status = detail.get("status")
        if cs2dt_status is None:
            order.last_checked_at = datetime.now(timezone.utc)
            return

        new_status = CS2DT_STATUS_MAP.get(cs2dt_status, "unknown")

        if new_status == order.status:
            order.last_checked_at = datetime.now(timezone.utc)
            return

        old_status = order.status
        order.status = new_status
        order.last_checked_at = datetime.now(timezone.utc)

        # Обновляем связанный Purchase
        if order.purchase_id:
            from fluxio.db.models import Purchase
            from sqlalchemy import select
            result = await uow._session.execute(
                select(Purchase).where(Purchase.id == order.purchase_id)
            )
            purchase = result.scalar_one_or_none()
            if purchase:
                if new_status == "delivered":
                    purchase.status = "success"
                    purchase.delivered_at = datetime.now(timezone.utc)
                    self._total_delivered += 1
                    logger.info(
                        f"OrderTracker: ордер {order_id} ДОСТАВЛЕН "
                        f"({purchase.market_hash_name})"
                    )
                elif new_status == "failed":
                    purchase.status = "failed"
                    purchase.notes = f"CS2DT статус: {cs2dt_status}"
                    self._total_failed += 1
                    logger.warning(
                        f"OrderTracker: ордер {order_id} НЕУДАЧА "
                        f"({purchase.market_hash_name})"
                    )
                else:
                    logger.debug(
                        f"OrderTracker: ордер {order_id} {old_status} → {new_status}"
                    )

    def stats(self) -> dict:
        """Статистика трекера для дашборда."""
        return {
            "total_delivered": self._total_delivered,
            "total_failed": self._total_failed,
            "total_cancelled": self._total_cancelled,
        }
