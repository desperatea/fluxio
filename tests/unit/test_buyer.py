"""Тесты для BuyerWorker — базовая логика.

Тестирует:
- Пустой список кандидатов → ничего не происходит
- Кандидат без cs2dt_item_id → пропуск
- Дисконт слишком низкий → пропуск
- Dry-run покупка → запись в БД с dry_run=True
- Pub/Sub уведомление при покупке
- Статистика
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Хелперы ─────────────────────────────────────────────────────────────────


def _make_item(**overrides) -> MagicMock:
    """Мок Item из БД."""
    item = MagicMock()
    defaults = {
        "market_hash_name": "Test Item",
        "item_name": "Test Item",
        "price_usd": 0.50,
        "steam_price_usd": 2.0,
        "steam_volume_24h": 50,
        "steam_median_30d": 1.5,
        "cs2dt_item_id": 99999,
        "app_id": 570,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(item, k, v)
    return item


def _make_redis() -> AsyncMock:
    """Мок Redis."""
    redis = AsyncMock()
    redis.smembers = AsyncMock(return_value=set())
    redis.sismember = AsyncMock(return_value=False)
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=1)
    redis.publish = AsyncMock(return_value=1)
    return redis


def _make_uow(item=None) -> AsyncMock:
    """Мок UnitOfWork."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.items = AsyncMock()
    uow.items.get_by_name = AsyncMock(return_value=item)
    uow.purchases = AsyncMock()
    uow.purchases.get_same_item_count_24h = AsyncMock(return_value=0)
    uow.purchases.exists = AsyncMock(return_value=False)

    mock_purchase = MagicMock()
    mock_purchase.id = 1
    mock_purchase.status = "pending"
    uow.purchases.create = AsyncMock(return_value=mock_purchase)
    uow.purchases.save_active_order = AsyncMock()
    uow.commit = AsyncMock()
    uow._session = AsyncMock()
    return uow


def _make_cs2dt() -> AsyncMock:
    """Мок CS2DT клиента."""
    client = AsyncMock()
    client.buy = AsyncMock(return_value={
        "buyPrice": 0.50,
        "orderId": "ORD123",
        "delivery": 2,
        "offerId": None,
    })
    client.get_balance = AsyncMock(return_value={"data": 100.0})
    client.get_prices_batch = AsyncMock(return_value=[])
    client.get_sell_list = AsyncMock(return_value={"list": []})
    return client


# ─── Тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buyer_empty_candidates():
    """Пустой список кандидатов → ничего не происходит."""
    redis = _make_redis()
    redis.smembers.return_value = set()
    cs2dt = _make_cs2dt()

    with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(cs2dt)
        await buyer.run_cycle()

    assert buyer._last_cycle_bought == 0
    cs2dt.get_prices_batch.assert_not_called()


@pytest.mark.asyncio
async def test_buyer_item_not_in_db():
    """Кандидат не найден в БД → пропуск."""
    redis = _make_redis()
    uow = _make_uow(None)
    cs2dt = _make_cs2dt()
    cs2dt.get_prices_batch.return_value = [{
        "marketHashName": "Missing Item",
        "price": 0.50,
        "quantity": 5,
    }]
    redis.smembers.return_value = {"Missing Item"}

    with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 1.0
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(cs2dt)
        await buyer.run_cycle()

    redis.srem.assert_called()
    assert buyer._last_cycle_skipped == 1


@pytest.mark.asyncio
async def test_buyer_low_discount_skipped():
    """Дисконт слишком низкий → пропуск."""
    # steam_price=1.2, cs2dt=1.0 → net=1.044, discount=4.2% < 45%
    item = _make_item(steam_price_usd=1.2, price_usd=1.0, steam_median_30d=1.0)
    redis = _make_redis()
    uow = _make_uow(item)
    cs2dt = _make_cs2dt()
    cs2dt.get_prices_batch.return_value = [{
        "marketHashName": "Test Item",
        "price": 1.0,
        "quantity": 5,
    }]
    redis.smembers.return_value = {"Test Item"}

    with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.min_discount_percent = 45
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 5.0
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(cs2dt)
        await buyer.run_cycle()

    assert buyer._last_cycle_skipped == 1
    uow.purchases.create.assert_not_called()


@pytest.mark.asyncio
async def test_buyer_dry_run_purchase():
    """Dry-run покупка — полная симуляция."""
    item = _make_item(steam_price_usd=2.0, price_usd=0.50, steam_median_30d=1.5)
    redis = _make_redis()
    uow = _make_uow(item)
    cs2dt = _make_cs2dt()
    cs2dt.get_prices_batch.return_value = [{
        "marketHashName": "Test Item",
        "price": 0.50,
        "quantity": 5,
    }]
    cs2dt.get_sell_list.return_value = {
        "list": [{"id": "prod1", "price": 0.50}],
    }
    redis.smembers.return_value = {"Test Item"}

    from fluxio.core.safety import SafetyCheck

    with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.buyer.run_all_checks", return_value=SafetyCheck(True)), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.dry_run = True
        mock_config.trading.min_discount_percent = 45
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 5.0
        mock_config.trading.max_same_item_count = 15
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.env.trade_url = "https://steamcommunity.com/tradeoffer/new/?partner=123"
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(cs2dt)
        await buyer.run_cycle()

    # Покупка создана
    uow.purchases.create.assert_called_once()
    call_kwargs = uow.purchases.create.call_args[1]
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["status"] == "pending"

    # CS2DT buy НЕ вызывается в dry-run
    cs2dt.buy.assert_not_called()

    # Purchase обновлён до success
    mock_purchase = uow.purchases.create.return_value
    assert mock_purchase.status == "success"
    assert "DRY-" in mock_purchase.order_id

    # Pub/Sub уведомление
    redis.publish.assert_called()

    assert buyer._total_bought == 1


@pytest.mark.asyncio
async def test_buyer_dry_run_no_trade_url():
    """Dry-run без TRADE_URL → покупка не проходит."""
    item = _make_item(steam_price_usd=2.0, price_usd=0.50, steam_median_30d=1.5)
    redis = _make_redis()
    uow = _make_uow(item)
    cs2dt = _make_cs2dt()
    cs2dt.get_prices_batch.return_value = [{
        "marketHashName": "Test Item",
        "price": 0.50,
        "quantity": 5,
    }]
    cs2dt.get_sell_list.return_value = {
        "list": [{"id": "prod1", "price": 0.50}],
    }
    redis.smembers.return_value = {"Test Item"}

    from fluxio.core.safety import SafetyCheck

    with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
         patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.buyer.run_all_checks", return_value=SafetyCheck(True)), \
         patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.trading.dry_run = True
        mock_config.trading.min_discount_percent = 45
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 5.0
        mock_config.trading.max_same_item_count = 15
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.update_queue.buyer_interval_seconds = 60
        mock_config.env.trade_url = ""  # Пустой
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(cs2dt)
        await buyer.run_cycle()

    uow.purchases.create.assert_not_called()
    assert buyer._total_bought == 0


@pytest.mark.asyncio
async def test_buyer_stats():
    """Метод stats() возвращает корректную статистику."""
    cs2dt = _make_cs2dt()
    with patch("fluxio.core.workers.buyer.config") as mock_config:
        mock_config.update_queue.buyer_interval_seconds = 60
        from fluxio.core.workers.buyer import BuyerWorker
        buyer = BuyerWorker(cs2dt)

    stats = buyer.stats()
    assert stats["total_bought"] == 0
    assert stats["total_spent_usd"] == 0
    assert "last_cycle_bought" in stats
    assert "last_cycle_skipped" in stats
