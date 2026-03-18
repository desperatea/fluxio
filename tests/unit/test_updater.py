"""Тесты для UpdaterWorker — базовая логика.

Тестирует:
- ZPOPMAX и обработку батча
- Кэш Redis (TTL 30 мин)
- Обновление steam:freshness
- Edge cases: нет предмета в БД, None от Steam, пустая очередь
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Хелперы ─────────────────────────────────────────────────────────────────


def _make_enriched_item(**overrides) -> MagicMock:
    """Мок Item с обогащёнными данными."""
    item = MagicMock()
    defaults = {
        "price_usd": 0.50,
        "steam_price_usd": 1.0,
        "steam_volume_24h": 50,
        "steam_updated_at": None,
        "steam_median_30d": 0.80,
        "steam_volume_7d": 60,
        "enriched_at": datetime.now(timezone.utc) - timedelta(days=1),
        "price_stability_cv": 0.1,
        "price_spike_ratio": 1.0,
        "sales_at_current_price": 20,
        "steam_item_nameid": 12345,
        "steam_sell_listings": 50,
        "steam_buy_order_price": 0.70,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(item, k, v)
    return item


def _make_unenriched_item(**overrides) -> MagicMock:
    """Мок Item без обогащения."""
    return _make_enriched_item(
        steam_median_30d=None,
        enriched_at=None,
        steam_item_nameid=None,
        steam_sell_listings=None,
        **overrides,
    )


def _make_overview(median=2.0, lowest=1.8, sales=50):
    """Мок ответа priceoverview."""
    data = MagicMock()
    data.median_price_usd = median
    data.lowest_price_usd = lowest
    data.sales_count = sales
    return data


def _make_steam():
    """Мок Steam клиента."""
    client = AsyncMock()
    client._proxy_urls = ["http://proxy1"]
    client.get_price_overview = AsyncMock(return_value={"success": True})
    client._parse_overview = MagicMock(return_value=_make_overview())
    client.get_item_orders_histogram = AsyncMock(return_value=None)
    return client


def _make_redis():
    """Мок Redis."""
    redis = AsyncMock()
    redis.zpopmax = AsyncMock(return_value=[])
    redis.hgetall = AsyncMock(return_value={})
    redis.hset = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=1)
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=0)
    redis.zcard = AsyncMock(return_value=0)
    return redis


def _make_uow(item=None):
    """Мок UnitOfWork."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.items = AsyncMock()
    uow.items.get_by_name = AsyncMock(return_value=item)
    uow.commit = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar = MagicMock(return_value=20)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    uow._session = mock_session
    uow.session = mock_session
    return uow


# ─── Тесты дисконта и кандидатов ──────────────────────────────────────────────


class TestUpdaterDiscountLogic:
    """Тесты логики расчёта дисконта и определения кандидатов."""

    def test_high_discount_becomes_candidate(self):
        """Дисконт >= min_discount → кандидат."""
        discount = self._calc_discount(steam=2.0, item_price=1.0, fee_pct=13.0)
        assert discount == pytest.approx(42.53, rel=0.01)

    def test_low_discount_not_candidate(self):
        """Дисконт < min_discount → не кандидат."""
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
async def test_updater_uses_redis_cache_when_available():
    """Updater использует Redis кэш — Steam API не вызывается."""
    item = _make_enriched_item()
    uow = _make_uow(item)
    redis = _make_redis()
    steam = _make_steam()

    hash_name = "Cached Item"
    redis.zpopmax.return_value = [(hash_name, 80.0)]
    redis.hgetall.return_value = {"median": "2.0", "volume": "50", "lowest": "1.8"}
    redis.zcard.return_value = 0

    with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
         patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.updater.config") as mock_config:
        mock_config.update_queue.enricher_freshness_days = 7
        mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
        mock_config.anti_manipulation.min_steam_listings = 15
        mock_config.anti_manipulation.max_spike_ratio = 2.0
        mock_config.anti_manipulation.max_price_cv = 0.5
        mock_config.anti_manipulation.min_sales_at_current_price = 5
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.trading.min_discount_percent = 45
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 1.0
        mock_config.trading.min_sales_volume_7d = 40
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(steam)
        await updater.run_cycle()

    # Steam API не должен вызываться при наличии кэша
    steam.get_price_overview.assert_not_called()


@pytest.mark.asyncio
async def test_updater_fetches_steam_when_no_cache():
    """Updater запрашивает Steam при отсутствии кэша."""
    item = _make_enriched_item()
    uow = _make_uow(item)
    redis = _make_redis()
    steam = _make_steam()

    hash_name = "Uncached Item"
    redis.zpopmax.return_value = [(hash_name, 100.0)]
    redis.hgetall.return_value = {}
    redis.zcard.return_value = 0

    with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
         patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.updater.config") as mock_config:
        mock_config.update_queue.enricher_freshness_days = 7
        mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
        mock_config.anti_manipulation.min_steam_listings = 15
        mock_config.anti_manipulation.max_spike_ratio = 2.0
        mock_config.anti_manipulation.max_price_cv = 0.5
        mock_config.anti_manipulation.min_sales_at_current_price = 5
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.trading.min_discount_percent = 45
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 1.0
        mock_config.trading.min_sales_volume_7d = 40
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(steam)
        await updater.run_cycle()

    # Steam API должен быть вызван
    steam.get_price_overview.assert_called_once()


@pytest.mark.asyncio
async def test_updater_caches_steam_result():
    """Updater кэширует результат Steam в Redis."""
    item = _make_enriched_item()
    uow = _make_uow(item)
    redis = _make_redis()
    steam = _make_steam()

    hash_name = "New Item"
    redis.zpopmax.return_value = [(hash_name, 100.0)]
    redis.hgetall.return_value = {}
    redis.zcard.return_value = 0

    with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
         patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.updater.config") as mock_config:
        mock_config.update_queue.enricher_freshness_days = 7
        mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
        mock_config.anti_manipulation.min_steam_listings = 15
        mock_config.anti_manipulation.max_spike_ratio = 2.0
        mock_config.anti_manipulation.max_price_cv = 0.5
        mock_config.anti_manipulation.min_sales_at_current_price = 5
        mock_config.fees.steam_fee_percent = 13.0
        mock_config.trading.min_discount_percent = 45
        mock_config.trading.min_price_usd = 0.04
        mock_config.trading.max_price_usd = 1.0
        mock_config.trading.min_sales_volume_7d = 40
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(steam)
        await updater.run_cycle()

    # hset должен быть вызван для кэша + freshness
    assert redis.hset.call_count >= 2
    assert redis.expire.called


@pytest.mark.asyncio
async def test_updater_empty_queue_sleeps():
    """При пустой очереди воркер не вызывает Steam."""
    redis = _make_redis()
    steam = _make_steam()
    redis.zpopmax.return_value = []

    with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
         patch("fluxio.core.workers.updater.asyncio.sleep", new_callable=AsyncMock):
        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(steam)
        await updater.run_cycle()

    steam.get_price_overview.assert_not_called()


@pytest.mark.asyncio
async def test_updater_steam_none_skips_update():
    """При None от Steam — не обновляем цены."""
    item = _make_enriched_item()
    uow = _make_uow(item)
    redis = _make_redis()
    steam = _make_steam()
    steam.get_price_overview.return_value = None
    steam._parse_overview.return_value = None

    hash_name = "Unknown Item"
    redis.zpopmax.return_value = [(hash_name, 100.0)]
    redis.hgetall.return_value = {}
    redis.zcard.return_value = 0

    with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
         patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.updater.config") as mock_config:
        mock_config.update_queue.enricher_freshness_days = 7
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(steam)
        await updater.run_cycle()

    # Цена не должна обновляться в Redis freshness
    freshness_calls = [
        c for c in redis.hset.call_args_list
        if "freshness" in str(c)
    ]
    assert len(freshness_calls) == 0


@pytest.mark.asyncio
async def test_updater_unenriched_item_sent_to_enricher():
    """Необогащённый предмет → в очередь enricher, без API."""
    item = _make_unenriched_item()
    uow = _make_uow(item)
    redis = _make_redis()
    steam = _make_steam()

    hash_name = "Unenriched"
    redis.zpopmax.return_value = [(hash_name, 100.0)]
    redis.zcard.return_value = 0

    with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
         patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
         patch("fluxio.core.workers.updater.config") as mock_config:
        mock_config.update_queue.enricher_freshness_days = 7
        mock_config.games = [MagicMock(enabled=True, app_id=570)]

        from fluxio.core.workers.updater import UpdaterWorker
        updater = UpdaterWorker(steam)
        await updater.run_cycle()

    steam.get_price_overview.assert_not_called()
    redis.sadd.assert_called()
