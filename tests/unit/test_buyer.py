"""Тесты для BuyerWorker — Фаза 3.

Тестирует:
- Пустой список кандидатов → ничего не происходит
- Кандидат без item_id → пропуск
- Кандидат уже куплен (Redis) → пропуск
- Стратегия отклонила → пропуск
- Safety check не пройден → пропуск
- Dry-run покупка → запись в БД с dry_run=True
- Pub/Sub уведомление при покупке
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_cs2dt():
    """Мок CS2DT клиента."""
    client = AsyncMock()
    client.buy = AsyncMock(return_value={
        "buyPrice": 1.0,
        "orderId": "ORD123",
        "delivery": 2,
        "offerId": None,
    })
    client.get_balance = AsyncMock(return_value={"data": 100.0})
    return client


@pytest.fixture
def mock_redis():
    """Мок Redis клиента."""
    redis = AsyncMock()
    redis.smembers = AsyncMock(return_value=set())
    redis.sismember = AsyncMock(return_value=False)
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=1)
    redis.publish = AsyncMock(return_value=1)
    return redis


@pytest.fixture
def mock_item():
    """Мок Item из БД."""
    item = MagicMock()
    item.item_id = "12345"
    item.market_hash_name = "Test Item"
    item.item_name = "Test Item"
    item.price_usd = 1.0
    item.steam_price_usd = 2.0
    item.steam_volume_24h = 50
    item.app_id = 570
    return item


@pytest.fixture
def mock_uow():
    """Мок UnitOfWork."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.items = AsyncMock()
    uow.purchases = AsyncMock()
    uow.commit = AsyncMock()

    mock_purchase = MagicMock()
    mock_purchase.id = 1
    mock_purchase.status = "pending"
    uow.purchases.create = AsyncMock(return_value=mock_purchase)
    uow.purchases.save_active_order = AsyncMock()
    return uow


# ─── Тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buyer_empty_candidates(mock_cs2dt, mock_redis):
    """Пустой список кандидатов → ничего не происходит."""
    mock_redis.smembers.return_value = set()

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis):
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    assert buyer._last_cycle_bought == 0
    assert buyer._last_cycle_skipped == 0


@pytest.mark.asyncio
async def test_buyer_item_not_in_db(mock_cs2dt, mock_redis, mock_uow):
    """Кандидат не найден в БД → удаление из кандидатов."""
    mock_redis.smembers.return_value = {"Missing Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=None)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    mock_redis.srem.assert_called()
    assert buyer._last_cycle_skipped == 1


@pytest.mark.asyncio
async def test_buyer_already_purchased_redis(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Предмет уже куплен (Redis SISMEMBER) → пропуск."""
    mock_redis.smembers.return_value = {"Test Item"}
    mock_redis.sismember = AsyncMock(return_value=True)
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    mock_redis.srem.assert_called()
    assert buyer._last_cycle_skipped == 1


@pytest.mark.asyncio
async def test_buyer_no_item_id_skipped(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Предмет без item_id → пропуск."""
    mock_item.item_id = None
    mock_redis.smembers.return_value = {"Test Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    assert buyer._last_cycle_skipped == 1


@pytest.mark.asyncio
async def test_buyer_strategy_rejects(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Стратегия отклоняет предмет → пропуск."""
    # Дисконт слишком низкий: steam=1.2, cs2dt=1.0 → ~4%
    mock_item.steam_price_usd = 1.2
    mock_item.price_usd = 1.0
    mock_redis.smembers.return_value = {"Test Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow):
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    assert buyer._last_cycle_skipped == 1
    # Покупка не должна создаваться
    mock_uow.purchases.create.assert_not_called()


@pytest.mark.asyncio
async def test_buyer_safety_check_fails(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Safety check не прошёл → пропуск."""
    mock_redis.smembers.return_value = {"Test Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)

    # Мокаем safety check → не пройден
    from fluxio.core.safety import SafetyCheck
    failed_check = SafetyCheck(False, "Дневной лимит превышен")

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow), \
         patch("fluxio.core.workers.buyer.run_all_checks", return_value=failed_check):
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    assert buyer._last_cycle_skipped == 1
    mock_uow.purchases.create.assert_not_called()


@pytest.mark.asyncio
async def test_buyer_dry_run_purchase(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Dry-run покупка — полная симуляция: баланс, прибыль, pending→success."""
    mock_redis.smembers.return_value = {"Test Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)

    from fluxio.core.safety import SafetyCheck
    passed_check = SafetyCheck(True)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow), \
         patch("fluxio.core.workers.buyer.run_all_checks", return_value=passed_check), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.dry_run = True
        mock_config.trading.min_discount_percent = 15
        mock_config.trading.min_price_usd = 0.10
        mock_config.trading.max_price_usd = 5.0
        mock_config.trading.min_sales_volume_7d = 10
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.blacklist.items = []
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.env.trade_url = "https://steamcommunity.com/tradeoffer/new/?partner=123"

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    # Покупка создана с dry_run=True, status=pending (как live)
    mock_uow.purchases.create.assert_called_once()
    call_kwargs = mock_uow.purchases.create.call_args[1]
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["status"] == "pending"

    # Баланс проверяется через CS2DT API
    mock_cs2dt.get_balance.assert_called_once()

    # CS2DT buy НЕ должен вызываться
    mock_cs2dt.buy.assert_not_called()

    # Purchase обновлён до success (через mock purchase object)
    mock_purchase = mock_uow.purchases.create.return_value
    assert mock_purchase.status == "success"
    assert mock_purchase.order_id is not None
    assert "DRY-" in mock_purchase.order_id

    # Redis: SADD purchased_ids + SREM candidates
    mock_redis.sadd.assert_called()
    mock_redis.srem.assert_called()

    # Pub/Sub уведомление
    mock_redis.publish.assert_called()

    # commit вызван минимум 2 раза (pending + success)
    assert mock_uow.commit.call_count >= 2

    assert buyer._total_bought == 1


@pytest.mark.asyncio
async def test_buyer_dry_run_no_trade_url(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Dry-run без TRADE_URL → покупка не проходит."""
    mock_redis.smembers.return_value = {"Test Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)

    from fluxio.core.safety import SafetyCheck
    passed_check = SafetyCheck(True)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow), \
         patch("fluxio.core.workers.buyer.run_all_checks", return_value=passed_check), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.dry_run = True
        mock_config.trading.min_discount_percent = 15
        mock_config.trading.min_price_usd = 0.10
        mock_config.trading.max_price_usd = 5.0
        mock_config.trading.min_sales_volume_7d = 10
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.blacklist.items = []
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.env.trade_url = ""  # Пустой TRADE_URL

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    # Покупка не должна создаваться
    mock_uow.purchases.create.assert_not_called()
    assert buyer._total_bought == 0


@pytest.mark.asyncio
async def test_buyer_dry_run_insufficient_balance(mock_cs2dt, mock_redis, mock_uow, mock_item):
    """Dry-run: баланс < цены → покупка не проходит."""
    mock_redis.smembers.return_value = {"Test Item"}
    mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)
    mock_cs2dt.get_balance.return_value = {"data": 0.50}  # Меньше чем price_usd=1.0

    from fluxio.core.safety import SafetyCheck
    passed_check = SafetyCheck(True)

    with patch("fluxio.core.workers.buyer.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=mock_uow), \
         patch("fluxio.core.workers.buyer.run_all_checks", return_value=passed_check), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.dry_run = True
        mock_config.trading.min_discount_percent = 15
        mock_config.trading.min_price_usd = 0.10
        mock_config.trading.max_price_usd = 5.0
        mock_config.trading.min_sales_volume_7d = 10
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.blacklist.items = []
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.env.trade_url = "https://steamcommunity.com/tradeoffer/new/?partner=123"

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)
        await buyer.run_cycle()

    # Баланс проверен
    mock_cs2dt.get_balance.assert_called_once()
    # Покупка не должна создаваться — недостаточно средств
    mock_uow.purchases.create.assert_not_called()
    assert buyer._total_bought == 0


@pytest.mark.asyncio
async def test_buyer_stats(mock_cs2dt):
    """Метод stats() возвращает корректную статистику."""
    with patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.update_queue.buyer_interval_seconds = 60
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(mock_cs2dt)

    stats = buyer.stats()
    assert stats["total_bought"] == 0
    assert stats["total_spent_usd"] == 0
    assert "last_cycle_bought" in stats
    assert "last_cycle_skipped" in stats
