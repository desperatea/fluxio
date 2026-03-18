"""Тесты для OrderTrackerWorker — Фаза 3.

Тестирует:
- Нет активных заказов → ничего не происходит
- Заказ доставлен (status=10) → обновление статуса
- Заказ неудачен (status=11) → обновление статуса
- Зависший P2P заказ (>12ч) → автоотмена
- Ошибка API → заказ остаётся pending
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_cs2dt():
    """Мок CS2DT клиента."""
    client = AsyncMock()
    client.get_order_detail = AsyncMock(return_value={"status": 10})
    client.cancel_order = AsyncMock(return_value={"code": 0})
    return client


def _make_order(
    order_id: str = "ORD123",
    purchase_id: int = 1,
    status: str = "pending",
    hours_ago: float = 0.5,
) -> MagicMock:
    """Создать мок ActiveOrder."""
    order = MagicMock()
    order.order_id = order_id
    order.purchase_id = purchase_id
    order.status = status
    order.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    order.last_checked_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    return order


def _make_purchase(
    purchase_id: int = 1,
    status: str = "pending",
    market_hash_name: str = "Test Item",
) -> MagicMock:
    """Создать мок Purchase."""
    p = MagicMock()
    p.id = purchase_id
    p.status = status
    p.market_hash_name = market_hash_name
    p.delivered_at = None
    p.notes = None
    return p


def _mock_session_with_purchase(purchase: MagicMock | None = None) -> AsyncMock:
    """Создать мок session.execute() который возвращает purchase."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = purchase
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
def mock_uow():
    """Мок UnitOfWork."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.purchases = AsyncMock()
    uow.purchases.get_stale_orders = AsyncMock(return_value=[])
    uow.commit = AsyncMock()
    uow._session = _mock_session_with_purchase(None)
    uow.session = uow._session
    return uow


# ─── Тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tracker_no_active_orders(mock_cs2dt, mock_uow):
    """Нет активных заказов → ничего не делает."""
    mock_uow.purchases.get_stale_orders.return_value = []

    with patch("fluxio.core.workers.order_tracker.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        tracker = OrderTrackerWorker(mock_cs2dt)
        await tracker.run_cycle()

    mock_cs2dt.get_order_detail.assert_not_called()


@pytest.mark.asyncio
async def test_tracker_order_delivered(mock_cs2dt, mock_uow):
    """Заказ доставлен (CS2DT status=10) → статус обновляется."""
    order = _make_order(hours_ago=0.5)
    purchase = _make_purchase()
    mock_uow.purchases.get_stale_orders.return_value = [order]
    mock_cs2dt.get_order_detail.return_value = {"status": 10}
    mock_uow._session = _mock_session_with_purchase(purchase)
    mock_uow.session = mock_uow._session

    with patch("fluxio.core.workers.order_tracker.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        tracker = OrderTrackerWorker(mock_cs2dt)
        await tracker.run_cycle()

    assert order.status == "delivered"
    assert purchase.status == "success"
    assert purchase.delivered_at is not None
    assert tracker._total_delivered == 1


@pytest.mark.asyncio
async def test_tracker_order_failed(mock_cs2dt, mock_uow):
    """Заказ неудачен (CS2DT status=11) → статус обновляется."""
    order = _make_order(hours_ago=0.5)
    purchase = _make_purchase()
    mock_uow.purchases.get_stale_orders.return_value = [order]
    mock_cs2dt.get_order_detail.return_value = {"status": 11}
    mock_uow._session = _mock_session_with_purchase(purchase)
    mock_uow.session = mock_uow._session

    with patch("fluxio.core.workers.order_tracker.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        tracker = OrderTrackerWorker(mock_cs2dt)
        await tracker.run_cycle()

    assert order.status == "failed"
    assert purchase.status == "failed"
    assert tracker._total_failed == 1


@pytest.mark.asyncio
async def test_tracker_stale_p2p_auto_cancel(mock_cs2dt, mock_uow):
    """Зависший P2P заказ (>12ч) → автоотмена."""
    order = _make_order(hours_ago=13.0)  # >12ч
    purchase = _make_purchase()
    mock_uow.purchases.get_stale_orders.return_value = [order]
    mock_uow._session = _mock_session_with_purchase(purchase)
    mock_uow.session = mock_uow._session

    with patch("fluxio.core.workers.order_tracker.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        tracker = OrderTrackerWorker(mock_cs2dt)
        await tracker.run_cycle()

    # Заказ должен быть отменён через CS2DT
    mock_cs2dt.cancel_order.assert_called_once_with("ORD123")
    assert order.status == "cancelled"
    assert purchase.status == "cancelled"
    assert tracker._total_cancelled == 1


@pytest.mark.asyncio
async def test_tracker_api_error_keeps_pending(mock_cs2dt, mock_uow):
    """Ошибка API → заказ остаётся pending."""
    order = _make_order(hours_ago=0.5)
    mock_uow.purchases.get_stale_orders.return_value = [order]
    mock_cs2dt.get_order_detail.side_effect = Exception("API timeout")

    with patch("fluxio.core.workers.order_tracker.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        tracker = OrderTrackerWorker(mock_cs2dt)
        await tracker.run_cycle()

    # Статус не меняется
    assert order.status == "pending"
    # last_checked_at обновляется
    assert order.last_checked_at is not None


@pytest.mark.asyncio
async def test_tracker_stats(mock_cs2dt):
    """Метод stats() возвращает корректную статистику."""
    from fluxio.core.workers.order_tracker import OrderTrackerWorker
    tracker = OrderTrackerWorker(mock_cs2dt)

    stats = tracker.stats()
    assert stats["total_delivered"] == 0
    assert stats["total_failed"] == 0
    assert stats["total_cancelled"] == 0


@pytest.mark.asyncio
async def test_tracker_same_status_no_update(mock_cs2dt, mock_uow):
    """CS2DT возвращает тот же статус → только last_checked_at обновляется."""
    order = _make_order(hours_ago=0.5, status="pending")
    mock_uow.purchases.get_stale_orders.return_value = [order]
    # status=1 → "pending" (тот же)
    mock_cs2dt.get_order_detail.return_value = {"status": 1}

    with patch("fluxio.core.workers.order_tracker.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        tracker = OrderTrackerWorker(mock_cs2dt)
        await tracker.run_cycle()

    # Статус не поменялся, Purchase не должен обновляться
    assert order.status == "pending"
    mock_uow.session.execute.assert_not_called()
