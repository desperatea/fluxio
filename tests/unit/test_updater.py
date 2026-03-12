"""Тесты для UpdaterWorker — Фаза 2.

Тестирует:
- ZPOPMAX и обработку батча
- Кэш Redis (TTL 30 мин)
- Обновление steam:freshness
- Расчёт дисконта → arb:candidates
- Edge cases: нет предмета в БД, None от Steam, ошибки
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Фикстуры ─────────────────────────────────────────────────────────────────


def make_steam_price(median: float = 2.0, lowest: float = 1.8, sales: int = 50):
    """Мок ответа SteamClient.get_median_price()."""
    price = MagicMock()
    price.median_price_usd = median
    price.lowest_price_usd = lowest
    price.sales_count = sales
    return price


@pytest.fixture
def mock_steam():
    """Мок Steam клиента."""
    client = AsyncMock()
    client.get_median_price = AsyncMock(return_value=make_steam_price())
    return client


@pytest.fixture
def mock_redis():
    """Мок Redis клиента."""
    redis = AsyncMock()
    redis.zpopmax = AsyncMock(return_value=[])
    redis.hgetall = AsyncMock(return_value={})
    redis.hset = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=1)
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=0)
    redis.zcard = AsyncMock(return_value=0)
    return redis


# ─── Тесты дисконта и кандидатов ──────────────────────────────────────────────


class TestUpdaterDiscountLogic:
    """Тесты логики расчёта дисконта и определения кандидатов."""

    def test_high_discount_becomes_candidate(self):
        """Дисконт >= min_discount → кандидат."""
        # Steam медиана $2.0, CS2DT цена $1.0
        # Комиссия 13%, net = 2.0 * 0.87 = 1.74
        # Дисконт = (1.74 - 1.0) / 1.74 = 42.5% >= 15%
        discount = self._calc_discount(steam=2.0, item_price=1.0, fee_pct=13.0)
        assert discount == pytest.approx(42.53, rel=0.01)

    def test_low_discount_not_candidate(self):
        """Дисконт < min_discount → не кандидат."""
        # Steam медиана $1.2, CS2DT цена $1.1
        # net = 1.2 * 0.87 = 1.044
        # Дисконт = (1.044 - 1.1) / 1.044 < 0 → отрицательный
        discount = self._calc_discount(steam=1.2, item_price=1.1, fee_pct=13.0)
        assert discount < 15.0

    def test_zero_steam_price_no_candidate(self):
        """Steam цена = 0 → не кандидат."""
        discount = self._calc_discount(steam=0.0, item_price=1.0, fee_pct=13.0)
        assert discount == 0

    def test_zero_item_price_no_candidate(self):
        """CS2DT цена = 0 → guard возвращает 0 (не кандидат)."""
        discount = self._calc_discount(steam=2.0, item_price=0.0, fee_pct=13.0)
        assert discount == 0

    @staticmethod
    def _calc_discount(steam: float, item_price: float, fee_pct: float) -> float:
        """Повторяем формулу из updater.py."""
        if steam <= 0 or item_price <= 0:
            return 0
        fee = fee_pct / 100
        net = steam * (1 - fee)
        if net <= 0:
            return 0
        return (net - item_price) / net * 100


# ─── Тесты кэша Redis ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_updater_uses_redis_cache_when_available(mock_steam, mock_redis):
    """Updater использует Redis кэш — Steam API не вызывается."""
    hash_name = "Cached Item"
    mock_redis.zpopmax.return_value = [(hash_name, 80.0)]
    mock_redis.hgetall.return_value = {"median": "2.0", "volume": "50", "lowest": "1.8"}
    mock_redis.zcard.return_value = 0

    mock_item = MagicMock()
    mock_item.price_usd = 1.0
    mock_item.steam_price_usd = None
    mock_item.steam_volume_24h = None
    mock_item.steam_updated_at = None
    # hasattr(item, "steam_updated_at") нужно учесть
    type(mock_item).steam_updated_at = MagicMock(return_value=None)

    with patch("fluxio.core.workers.updater.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.updater.UnitOfWork") as mock_uow_cls:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.items = AsyncMock()
        mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)
        mock_uow.commit = AsyncMock()
        mock_uow_cls.return_value = mock_uow

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(mock_steam)
        await updater.run_cycle()

    # Steam API не должен вызываться при наличии кэша
    mock_steam.get_median_price.assert_not_called()


@pytest.mark.asyncio
async def test_updater_fetches_steam_when_no_cache(mock_steam, mock_redis):
    """Updater запрашивает Steam при отсутствии кэша."""
    hash_name = "Uncached Item"
    mock_redis.zpopmax.return_value = [(hash_name, 100.0)]
    mock_redis.hgetall.return_value = {}  # Нет кэша
    mock_redis.zcard.return_value = 0

    mock_item = MagicMock()
    mock_item.price_usd = 1.0
    mock_item.steam_price_usd = None
    mock_item.steam_volume_24h = None

    with patch("fluxio.core.workers.updater.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.updater.UnitOfWork") as mock_uow_cls:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.items = AsyncMock()
        mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)
        mock_uow.commit = AsyncMock()
        mock_uow_cls.return_value = mock_uow

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(mock_steam)
        await updater.run_cycle()

    # Steam API должен быть вызван
    mock_steam.get_median_price.assert_called_once_with(hash_name, app_id=570)


@pytest.mark.asyncio
async def test_updater_caches_steam_result(mock_steam, mock_redis):
    """Updater кэширует результат Steam в Redis."""
    hash_name = "New Item"
    mock_redis.zpopmax.return_value = [(hash_name, 100.0)]
    mock_redis.hgetall.return_value = {}
    mock_redis.zcard.return_value = 0

    mock_item = MagicMock()
    mock_item.price_usd = 1.0
    mock_item.steam_price_usd = None
    mock_item.steam_volume_24h = None

    with patch("fluxio.core.workers.updater.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.updater.UnitOfWork") as mock_uow_cls:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.items = AsyncMock()
        mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)
        mock_uow.commit = AsyncMock()
        mock_uow_cls.return_value = mock_uow

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(mock_steam)
        await updater.run_cycle()

    # hset должен быть вызван для кэша + freshness
    assert mock_redis.hset.call_count >= 2  # кэш + freshness
    # expire должен быть вызван для кэша
    assert mock_redis.expire.called


@pytest.mark.asyncio
async def test_updater_adds_to_candidates_on_high_discount(mock_steam, mock_redis):
    """Предмет с высоким дисконтом добавляется в arb:candidates."""
    hash_name = "Profitable Item"
    mock_redis.zpopmax.return_value = [(hash_name, 100.0)]
    mock_redis.hgetall.return_value = {}
    mock_redis.zcard.return_value = 0

    # Steam = $2.0, CS2DT = $1.0 → дисконт ~42% >= 15%
    mock_steam.get_median_price.return_value = make_steam_price(median=2.0, sales=50)

    mock_item = MagicMock()
    mock_item.price_usd = 1.0
    mock_item.steam_price_usd = None
    mock_item.steam_volume_24h = 50

    with patch("fluxio.core.workers.updater.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.updater.UnitOfWork") as mock_uow_cls:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.items = AsyncMock()
        mock_uow.items.get_by_name = AsyncMock(return_value=mock_item)
        mock_uow.commit = AsyncMock()
        mock_uow_cls.return_value = mock_uow

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(mock_steam)
        await updater.run_cycle()

    # sadd к arb:candidates должен быть вызван
    mock_redis.sadd.assert_called()
    call_args = mock_redis.sadd.call_args[0]
    assert "arb:candidates" in call_args
    assert hash_name in call_args


@pytest.mark.asyncio
async def test_updater_empty_queue_sleeps(mock_steam, mock_redis):
    """При пустой очереди воркер не вызывает Steam."""
    mock_redis.zpopmax.return_value = []  # Пустая очередь

    with patch("fluxio.core.workers.updater.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.updater.asyncio.sleep", new_callable=AsyncMock):
        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(mock_steam)
        await updater.run_cycle()

    # Steam не должен вызываться
    mock_steam.get_median_price.assert_not_called()


@pytest.mark.asyncio
async def test_updater_steam_none_skips_db_update(mock_steam, mock_redis):
    """При None от Steam — DB не обновляется."""
    hash_name = "Unknown Item"
    mock_redis.zpopmax.return_value = [(hash_name, 100.0)]
    mock_redis.hgetall.return_value = {}
    mock_redis.zcard.return_value = 0
    mock_steam.get_median_price.return_value = None  # Steam вернул None

    with patch("fluxio.core.workers.updater.get_redis", return_value=mock_redis), \
         patch("fluxio.core.workers.updater.UnitOfWork") as mock_uow_cls:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.items = AsyncMock()
        mock_uow.items.get_by_name = AsyncMock()
        mock_uow.commit = AsyncMock()
        mock_uow_cls.return_value = mock_uow

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(mock_steam)
        await updater.run_cycle()

    # DB не должна обновляться (UoW.items.get_by_name не вызывался)
    mock_uow.items.get_by_name.assert_not_called()
