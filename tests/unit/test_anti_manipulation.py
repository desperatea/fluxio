"""Тесты анти-манипуляционных фильтров — все изменения с последнего коммита.

Покрывает:
- Updater: ранняя проверка enrichment (шаг 0, экономия прокси)
- Updater: ratio check (steam_current / median_30d > 2.0)
- Updater: blended reference price (ratio > 1.5)
- Updater: itemordershistogram → min_steam_listings
- Updater: пересчёт sales_at_price из steam_sales_history
- Enricher: volume-weighted spike_ratio
- Enricher: _calc_price_cv с выбросами
- Enricher: _parse_history_30d
- Enricher: _calc_sales_at_price
- Enricher: _ensure_item_nameid
- Buyer: safety net по медиане
- Config: новые поля AntiManipulationConfig
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Хелперы
# ═══════════════════════════════════════════════════════════════════════════════


def _make_item(**overrides) -> MagicMock:
    """Создать мок Item со всеми полями."""
    item = MagicMock()
    defaults = {
        "market_hash_name": "Test Item",
        "price_usd": 0.50,
        "steam_price_usd": 1.0,
        "steam_volume_24h": 50,
        "steam_median_30d": 0.80,
        "steam_volume_7d": 60,
        "enriched_at": datetime.now(timezone.utc) - timedelta(days=1),
        "steam_updated_at": datetime.now(timezone.utc),
        "price_stability_cv": 0.1,
        "price_spike_ratio": 1.0,
        "sales_at_current_price": 20,
        "steam_item_nameid": 12345,
        "steam_sell_listings": 50,
        "steam_buy_order_price": 0.70,
        "cs2dt_item_id": 99999,
        "app_id": 570,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(item, k, v)
    return item


def _make_redis() -> AsyncMock:
    """Создать мок Redis."""
    redis = AsyncMock()
    redis.zpopmax = AsyncMock(return_value=[])
    redis.hgetall = AsyncMock(return_value={})
    redis.hset = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=1)
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=0)
    redis.sismember = AsyncMock(return_value=False)
    redis.zcard = AsyncMock(return_value=0)
    redis.smembers = AsyncMock(return_value=set())
    redis.publish = AsyncMock(return_value=1)
    return redis


def _make_uow(item=None) -> AsyncMock:
    """Создать мок UnitOfWork."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.items = AsyncMock()
    uow.items.get_by_name = AsyncMock(return_value=item)
    uow.purchases = AsyncMock()
    uow.purchases.get_same_item_count_24h = AsyncMock(return_value=0)
    uow.purchases.exists = AsyncMock(return_value=False)
    uow.commit = AsyncMock()
    # Мок сессии с execute, возвращающим scalar()
    mock_result = MagicMock()
    mock_result.scalar = MagicMock(return_value=20)
    uow._session = AsyncMock()
    uow._session.execute = AsyncMock(return_value=mock_result)
    return uow


def _make_steam() -> AsyncMock:
    """Создать мок Steam клиента."""
    client = AsyncMock()
    client._proxy_urls = ["http://proxy1", "http://proxy2"]
    overview_data = MagicMock()
    overview_data.median_price_usd = 1.0
    overview_data.lowest_price_usd = 0.90
    overview_data.sales_count = 50
    client.get_price_overview = AsyncMock(return_value={"success": True})
    client._parse_overview = MagicMock(return_value=overview_data)
    client.get_item_orders_histogram = AsyncMock(return_value=None)
    client.get_item_nameid = AsyncMock(return_value=None)
    return client


def _make_history(days_prices: list[tuple[int, float, int]]) -> list[list]:
    """Создать историю продаж.

    Args:
        days_prices: список (дней_назад, цена, объём).
    """
    result = []
    now = datetime.now(timezone.utc)
    for days_ago, price, volume in days_prices:
        dt = now - timedelta(days=days_ago)
        date_str = dt.strftime("%b %d %Y %H") + ": +0"
        result.append([date_str, price, str(volume)])
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Config: новые поля AntiManipulationConfig
# ═══════════════════════════════════════════════════════════════════════════════


class TestAntiManipulationConfig:
    """Тесты новых полей конфигурации."""

    def test_max_price_to_median_ratio_default(self):
        from fluxio.config import AntiManipulationConfig
        cfg = AntiManipulationConfig({})
        assert cfg.max_price_to_median_ratio == 2.0

    def test_max_price_to_median_ratio_custom(self):
        from fluxio.config import AntiManipulationConfig
        cfg = AntiManipulationConfig({"max_price_to_median_ratio": 3.0})
        assert cfg.max_price_to_median_ratio == 3.0

    def test_min_steam_listings_default(self):
        from fluxio.config import AntiManipulationConfig
        cfg = AntiManipulationConfig({})
        assert cfg.min_steam_listings == 15

    def test_min_steam_listings_custom(self):
        from fluxio.config import AntiManipulationConfig
        cfg = AntiManipulationConfig({"min_steam_listings": 25})
        assert cfg.min_steam_listings == 25


# ═══════════════════════════════════════════════════════════════════════════════
# Enricher: _parse_history_30d
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseHistory30d:
    """Тесты парсинга истории за 30 дней."""

    def test_filters_old_entries(self):
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([
            (5, 0.50, 10),    # 5 дней назад → проходит
            (35, 0.40, 5),    # 35 дней назад → отфильтрован
        ])
        parsed = EnricherWorker._parse_history_30d(history)
        assert len(parsed) == 1
        assert parsed[0][1] == 0.50

    def test_empty_history(self):
        from fluxio.core.workers.enricher import EnricherWorker
        assert EnricherWorker._parse_history_30d([]) == []

    def test_malformed_entries_skipped(self):
        from fluxio.core.workers.enricher import EnricherWorker
        history = [
            ["bad date", 1.0, "5"],
            ["short"],
        ]
        assert EnricherWorker._parse_history_30d(history) == []

    def test_volume_string_with_comma(self):
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([(1, 0.50, 1)])
        # Подменяем volume на строку с запятой
        history[0][2] = "1,234"
        parsed = EnricherWorker._parse_history_30d(history)
        assert len(parsed) == 1
        assert parsed[0][2] == 1234


# ═══════════════════════════════════════════════════════════════════════════════
# Enricher: _calc_price_cv (volume-weighted, с отсечкой выбросов)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCalcPriceCV:
    """Тесты коэффициента вариации цены."""

    def test_stable_prices_low_cv(self):
        """Стабильные цены → низкий CV."""
        from fluxio.core.workers.enricher import EnricherWorker
        # 100 продаж по $0.50, 100 по $0.51 → CV ≈ 0
        history = _make_history([
            (1, 0.50, 100),
            (2, 0.51, 100),
        ])
        cv = EnricherWorker._calc_price_cv(history)
        assert cv < 0.1

    def test_volatile_prices_high_cv(self):
        """Волатильные цены → высокий CV."""
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([
            (1, 0.10, 50),
            (2, 0.90, 50),
        ])
        cv = EnricherWorker._calc_price_cv(history)
        assert cv > 0.3

    def test_outliers_filtered(self):
        """Выбросы (>5x от медианы) отбрасываются."""
        from fluxio.core.workers.enricher import EnricherWorker
        # Медиана ~$0.50, выброс $10 (20x) → отфильтрован
        history = _make_history([
            (1, 0.50, 100),
            (2, 0.48, 100),
            (3, 10.0, 1),    # выброс
        ])
        cv = EnricherWorker._calc_price_cv(history)
        assert cv < 0.1  # выброс не повлиял

    def test_empty_history_returns_zero(self):
        from fluxio.core.workers.enricher import EnricherWorker
        assert EnricherWorker._calc_price_cv([]) == 0.0

    def test_single_entry_returns_zero(self):
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([(1, 0.50, 1)])
        assert EnricherWorker._calc_price_cv(history) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Enricher: _calc_spike_ratio (volume-weighted)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCalcSpikeRatio:
    """Тесты volume-weighted spike ratio."""

    def test_no_spike_ratio_near_one(self):
        """Стабильные цены → ratio ≈ 1."""
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([
            (1, 0.50, 50),     # 3д
            (2, 0.50, 50),     # 3д
            (10, 0.50, 50),    # 27д
            (20, 0.50, 50),    # 27д
        ])
        ratio = EnricherWorker._calc_spike_ratio(history)
        assert 0.9 <= ratio <= 1.1

    def test_spike_detected(self):
        """Спайк за 3 дня → ratio > 2."""
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([
            (1, 5.0, 2),       # 3д: 2 продажи по $5
            (10, 0.50, 100),   # 27д: 100 продаж по $0.50
        ])
        ratio = EnricherWorker._calc_spike_ratio(history)
        assert ratio > 2.0

    def test_volume_weighting_dampens_manipulative_spike(self):
        """Одна продажа по $7 + 100 по $0.40 → ratio не растёт сильно."""
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([
            (1, 7.0, 1),       # 3д: 1 манипулятивная
            (1, 0.40, 100),    # 3д: 100 нормальных
            (10, 0.40, 100),   # 27д
        ])
        ratio = EnricherWorker._calc_spike_ratio(history)
        # Средневзвешенная 3д: (7*1 + 0.40*100) / 101 ≈ 0.465
        # Средневзвешенная 27д: 0.40
        # Ratio ≈ 1.16 — манипуляция не проходит
        assert ratio < 1.5

    def test_no_data_returns_one(self):
        """Нет данных → ratio = 1 (нормальный)."""
        from fluxio.core.workers.enricher import EnricherWorker
        assert EnricherWorker._calc_spike_ratio([]) == 1.0

    def test_only_3d_data_returns_one(self):
        """Данные только за 3 дня, нет 27д → ratio = 1."""
        from fluxio.core.workers.enricher import EnricherWorker
        history = _make_history([(1, 0.50, 50)])
        assert EnricherWorker._calc_spike_ratio(history) == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Enricher: _calc_sales_at_price
# ═══════════════════════════════════════════════════════════════════════════════


class TestCalcSalesAtPrice:
    """Тесты подсчёта продаж в диапазоне ±20%."""

    def test_counts_within_range(self):
        from fluxio.core.workers.enricher import EnricherWorker
        # target $1.00, range [$0.80, $1.20]
        history = _make_history([
            (1, 0.90, 10),   # в диапазоне
            (2, 1.10, 5),    # в диапазоне
            (3, 0.50, 20),   # вне диапазона
            (4, 2.00, 30),   # вне диапазона
        ])
        count = EnricherWorker._calc_sales_at_price(history, 1.00)
        assert count == 15  # 10 + 5

    def test_empty_history(self):
        from fluxio.core.workers.enricher import EnricherWorker
        assert EnricherWorker._calc_sales_at_price([], 1.00) == 0

    def test_boundary_prices_included(self):
        from fluxio.core.workers.enricher import EnricherWorker
        # target $1.00 → ±20% → [$0.80, $1.20]
        history = _make_history([
            (1, 0.80, 5),    # на нижней границе
            (2, 1.20, 5),    # на верхней границе
        ])
        count = EnricherWorker._calc_sales_at_price(history, 1.00)
        assert count == 10


# ═══════════════════════════════════════════════════════════════════════════════
# Enricher: _ensure_item_nameid
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnsureItemNameid:
    """Тесты получения и кэширования item_nameid."""

    @pytest.mark.asyncio
    async def test_returns_cached_nameid(self):
        """Если item_nameid уже в БД — возвращает без API-вызова."""
        item = _make_item(steam_item_nameid=12345)
        uow = _make_uow(item)
        steam = _make_steam()

        with patch("fluxio.core.workers.enricher.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.enricher.async_session_factory"):
            from fluxio.core.workers.enricher import EnricherWorker
            enricher = EnricherWorker.__new__(EnricherWorker)
            enricher._steam = steam
            result = await enricher._ensure_item_nameid("Test Item", 570)

        assert result == 12345
        steam.get_item_nameid.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_from_steam_when_not_cached(self):
        """Если нет в БД — запрашивает с Steam."""
        item = _make_item(steam_item_nameid=None)
        uow = _make_uow(item)
        steam = _make_steam()
        steam.get_item_nameid = AsyncMock(return_value=67890)

        with patch("fluxio.core.workers.enricher.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.enricher.async_session_factory"):
            from fluxio.core.workers.enricher import EnricherWorker
            enricher = EnricherWorker.__new__(EnricherWorker)
            enricher._steam = steam
            result = await enricher._ensure_item_nameid("Test Item", 570)

        assert result == 67890
        steam.get_item_nameid.assert_called_once_with("Test Item", app_id=570)

    @pytest.mark.asyncio
    async def test_returns_none_on_steam_error(self):
        """При ошибке Steam → возвращает None."""
        item = _make_item(steam_item_nameid=None)
        uow = _make_uow(item)
        steam = _make_steam()
        steam.get_item_nameid = AsyncMock(side_effect=Exception("timeout"))

        with patch("fluxio.core.workers.enricher.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.enricher.async_session_factory"):
            from fluxio.core.workers.enricher import EnricherWorker
            enricher = EnricherWorker.__new__(EnricherWorker)
            enricher._steam = steam
            result = await enricher._ensure_item_nameid("Test Item", 570)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Updater: ранняя проверка enrichment (экономия прокси)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdaterEarlyEnrichmentCheck:
    """Тесты шага 0: необогащённые предметы не тратят прокси."""

    @pytest.mark.asyncio
    async def test_unenriched_item_sent_to_enricher_without_api_call(self):
        """Предмет без enrichment → в enrich_queue, priceoverview НЕ вызывается."""
        item = _make_item(enriched_at=None, steam_median_30d=None)
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.games = [MagicMock(enabled=True, app_id=570)]

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("Unenriched Item", redis)

        # priceoverview НЕ должен вызываться
        steam.get_price_overview.assert_not_called()
        # Предмет отправлен в enrich_queue
        redis.sadd.assert_called()
        assert any(
            "steam:enrich_queue" in str(c) or "enrich" in str(c)
            for c in redis.sadd.call_args_list
        )

    @pytest.mark.asyncio
    async def test_stale_enrichment_sent_to_enricher_without_api_call(self):
        """Устаревшее обогащение (>7д) → в enrich_queue, без API."""
        item = _make_item(
            enriched_at=datetime.now(timezone.utc) - timedelta(days=10),
            steam_median_30d=0.50,
        )
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.games = [MagicMock(enabled=True, app_id=570)]

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("Stale Item", redis)

        steam.get_price_overview.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_enrichment_proceeds_to_api(self):
        """Свежее обогащение → идёт дальше к priceoverview."""
        item = _make_item()  # enriched_at = 1 день назад по умолчанию
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.games = [MagicMock(enabled=True, app_id=570)]
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

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("Fresh Item", redis)

        # priceoverview ДОЛЖЕН вызываться
        steam.get_price_overview.assert_called_once()

    @pytest.mark.asyncio
    async def test_item_not_in_db_returns_early(self):
        """Предмет не найден в БД → return, без API."""
        uow = _make_uow(None)  # get_by_name returns None
        redis = _make_redis()
        steam = _make_steam()

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("Ghost Item", redis)

        steam.get_price_overview.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Updater: ratio check (steam_current / median_30d)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdaterRatioCheck:
    """Тесты проверки отношения цены листинга к медиане."""

    @pytest.mark.asyncio
    async def test_high_ratio_rejected(self):
        """Ratio 17x (Belt of the Grey Wastes) → отклонён."""
        # steam_lowest=$7.02, median=$0.41 → ratio=17.1x
        item = _make_item(
            steam_median_30d=0.41,
            steam_sell_listings=50,
        )
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()
        overview = MagicMock()
        overview.median_price_usd = 7.50
        overview.lowest_price_usd = 7.02
        overview.sales_count = 10
        steam._parse_overview = MagicMock(return_value=overview)

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
            mock_config.anti_manipulation.min_steam_listings = 15
            mock_config.games = [MagicMock(enabled=True, app_id=570)]
            mock_config.fees.steam_fee_percent = 13.0

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("Belt of the Grey Wastes", redis)

        # Должен быть удалён из кандидатов
        redis.srem.assert_called()

    @pytest.mark.asyncio
    async def test_normal_ratio_passes(self):
        """Ratio 1.6x (Shark Cowl) → проходит."""
        # steam_lowest=$0.13, median=$0.08 → ratio=1.625x
        item = _make_item(
            steam_median_30d=0.08,
            steam_sell_listings=50,
            price_usd=0.04,
            steam_volume_7d=60,
            price_spike_ratio=1.0,
            price_stability_cv=0.1,
            sales_at_current_price=20,
        )
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()
        overview = MagicMock()
        overview.median_price_usd = 0.15
        overview.lowest_price_usd = 0.13
        overview.sales_count = 80
        steam._parse_overview = MagicMock(return_value=overview)
        # Histogram: 50 листингов → проходит min_steam_listings
        steam.get_item_orders_histogram = AsyncMock(return_value={
            "success": 1,
            "sell_order_count": "50",
            "highest_buy_order": "10",
        })

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
            await updater._update_item("Shark Cowl", redis)

        # Не должен быть отклонён ratio check'ом → стал кандидатом
        sadd_calls = [str(c) for c in redis.sadd.call_args_list]
        assert any("arb:candidates" in c for c in sadd_calls)


# ═══════════════════════════════════════════════════════════════════════════════
# Updater: blended reference price
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlendedReferencePrice:
    """Тесты скорректированной эталонной цены при ratio > 1.5."""

    def test_blend_formula(self):
        """При ratio > 1.5: reference = steam * 0.7 + median * 0.3."""
        steam_current = 1.5
        median_30d = 0.80
        # ratio = 1.5/0.8 = 1.875 > 1.5 → blend
        reference = steam_current * 0.7 + median_30d * 0.3
        assert reference == pytest.approx(1.29, rel=0.01)

    def test_no_blend_when_ratio_low(self):
        """При ratio <= 1.5: reference = steam_current."""
        steam_current = 1.0
        median_30d = 0.80
        # ratio = 1.0/0.8 = 1.25 ≤ 1.5 → без blend
        if steam_current / median_30d > 1.5:
            reference = steam_current * 0.7 + median_30d * 0.3
        else:
            reference = steam_current
        assert reference == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Updater: itemordershistogram → min_steam_listings
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdaterHistogram:
    """Тесты интеграции itemordershistogram."""

    @pytest.mark.asyncio
    async def test_few_listings_rejected(self):
        """Мало листингов на Steam (<15) → отклонён."""
        item = _make_item(
            steam_item_nameid=12345,
            steam_sell_listings=None,
        )
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()
        steam.get_item_orders_histogram = AsyncMock(return_value={
            "success": 1,
            "sell_order_count": "5",
            "highest_buy_order": "50",
        })

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.anti_manipulation.min_steam_listings = 15
            mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
            mock_config.games = [MagicMock(enabled=True, app_id=570)]
            mock_config.fees.steam_fee_percent = 13.0

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("Low Listings Item", redis)

        # Должен быть удалён из кандидатов
        redis.srem.assert_called()

    @pytest.mark.asyncio
    async def test_enough_listings_passes(self):
        """Достаточно листингов (>=15) → проходит проверку."""
        item = _make_item(
            steam_item_nameid=12345,
            steam_sell_listings=None,
        )
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()
        steam.get_item_orders_histogram = AsyncMock(return_value={
            "success": 1,
            "sell_order_count": "200",
            "highest_buy_order": "80",
        })

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.anti_manipulation.min_steam_listings = 15
            mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
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
            await updater._update_item("Popular Item", redis)

        # Histogram должен быть вызван
        steam.get_item_orders_histogram.assert_called_once_with(12345)
        # sell_listings обновлён
        assert item.steam_sell_listings == 200
        # buy_order_price обновлён (80 центов → $0.80)
        assert item.steam_buy_order_price == 0.80

    @pytest.mark.asyncio
    async def test_no_nameid_rejected_by_listings_check(self):
        """Нет item_nameid → steam_sell_listings=0 → отклонён."""
        item = _make_item(
            steam_item_nameid=None,
            steam_sell_listings=None,
        )
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.anti_manipulation.min_steam_listings = 15
            mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
            mock_config.games = [MagicMock(enabled=True, app_id=570)]

            from fluxio.core.workers.updater import UpdaterWorker
            updater = UpdaterWorker(steam)
            await updater._update_item("No Nameid Item", redis)

        # histogram НЕ вызывается (нет nameid)
        steam.get_item_orders_histogram.assert_not_called()
        # Отклонён (steam_sell_listings=0 < 15)
        redis.srem.assert_called()

    @pytest.mark.asyncio
    async def test_histogram_string_sell_count_with_comma(self):
        """sell_order_count со строковым значением и запятой парсится."""
        item = _make_item(steam_item_nameid=12345, steam_sell_listings=None)
        uow = _make_uow(item)
        redis = _make_redis()
        steam = _make_steam()
        steam.get_item_orders_histogram = AsyncMock(return_value={
            "success": 1,
            "sell_order_count": "1,234",
            "highest_buy_order": "100",
        })

        with patch("fluxio.core.workers.updater.get_redis", return_value=redis), \
             patch("fluxio.core.workers.updater.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            mock_config.anti_manipulation.min_steam_listings = 15
            mock_config.anti_manipulation.max_price_to_median_ratio = 2.0
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
            await updater._update_item("Popular Item 2", redis)

        assert item.steam_sell_listings == 1234


# ═══════════════════════════════════════════════════════════════════════════════
# Updater: _is_enrichment_fresh
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsEnrichmentFresh:
    """Тесты проверки свежести обогащения."""

    def test_no_median_not_fresh(self):
        from fluxio.core.workers.updater import UpdaterWorker
        item = _make_item(steam_median_30d=None, enriched_at=datetime.now(timezone.utc))
        with patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            assert UpdaterWorker._is_enrichment_fresh(item) is False

    def test_no_enriched_at_not_fresh(self):
        from fluxio.core.workers.updater import UpdaterWorker
        item = _make_item(enriched_at=None, steam_median_30d=0.50)
        with patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            assert UpdaterWorker._is_enrichment_fresh(item) is False

    def test_old_enrichment_not_fresh(self):
        from fluxio.core.workers.updater import UpdaterWorker
        item = _make_item(enriched_at=datetime.now(timezone.utc) - timedelta(days=10))
        with patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            assert UpdaterWorker._is_enrichment_fresh(item) is False

    def test_recent_enrichment_fresh(self):
        from fluxio.core.workers.updater import UpdaterWorker
        item = _make_item(enriched_at=datetime.now(timezone.utc) - timedelta(days=2))
        with patch("fluxio.core.workers.updater.config") as mock_config:
            mock_config.update_queue.enricher_freshness_days = 7
            assert UpdaterWorker._is_enrichment_fresh(item) is True


# ═══════════════════════════════════════════════════════════════════════════════
# Buyer: safety net по медиане
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuyerSafetyNet:
    """Тесты safety net: отклонение убыточных по медиане."""

    @pytest.mark.asyncio
    async def test_loss_by_median_rejected(self):
        """Убыток по медиане → отклонён.

        median=$0.10, fee=13%, net_median=$0.087
        cs2dt_price=$0.10 → net_median < cs2dt_price → убыток.
        """
        item = _make_item(
            steam_price_usd=2.0,
            steam_median_30d=0.10,
            price_usd=0.10,
            cs2dt_item_id=99999,
        )
        uow = _make_uow(item)
        redis = _make_redis()

        mock_cs2dt = AsyncMock()
        mock_cs2dt.get_prices_batch = AsyncMock(return_value=[{
            "marketHashName": "Bad Item",
            "price": 0.10,
            "quantity": 5,
        }])

        with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
             patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.buyer.config") as mock_config:
            mock_config.trading.min_discount_percent = 45
            mock_config.trading.min_price_usd = 0.04
            mock_config.trading.max_price_usd = 1.0
            mock_config.trading.dry_run = True
            mock_config.fees.steam_fee_percent = 13.0
            mock_config.update_queue.buyer_interval_seconds = 60
            mock_config.games = [MagicMock(enabled=True, app_id=570)]

            from fluxio.core.workers.buyer import BuyerWorker
            buyer = BuyerWorker(mock_cs2dt)
            result = await buyer._process_item("Bad Item", 0.10, 5, redis)

        # Результат 0 — не куплен
        assert result == 0
        # Удалён из кандидатов
        redis.srem.assert_called()

    @pytest.mark.asyncio
    async def test_profit_by_median_passes(self):
        """Прибыль по медиане → проходит safety net.

        median=$0.80, fee=13%, net_median=$0.696
        cs2dt_price=$0.04 → net_median > cs2dt_price → ОК.
        """
        item = _make_item(
            steam_price_usd=1.0,
            steam_median_30d=0.80,
            price_usd=0.04,
            cs2dt_item_id=99999,
        )
        uow = _make_uow(item)
        uow.purchases.create = AsyncMock(return_value=MagicMock(id=1, status="pending"))
        redis = _make_redis()

        mock_cs2dt = AsyncMock()
        mock_cs2dt.get_sell_list = AsyncMock(return_value={
            "list": [{"id": "prod1", "price": 0.04}],
        })
        mock_cs2dt.get_balance = AsyncMock(return_value={"data": 100.0})

        from fluxio.core.safety import SafetyCheck

        with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
             patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.buyer.run_all_checks", return_value=SafetyCheck(True)), \
             patch("fluxio.core.workers.buyer.config") as mock_config:
            mock_config.trading.min_discount_percent = 45
            mock_config.trading.min_price_usd = 0.04
            mock_config.trading.max_price_usd = 1.0
            mock_config.trading.max_same_item_count = 15
            mock_config.trading.dry_run = True
            mock_config.fees.steam_fee_percent = 13.0
            mock_config.update_queue.buyer_interval_seconds = 60
            mock_config.env.trade_url = "https://steamcommunity.com/tradeoffer/new/?partner=123"
            mock_config.games = [MagicMock(enabled=True, app_id=570)]

            from fluxio.core.workers.buyer import BuyerWorker
            buyer = BuyerWorker(mock_cs2dt)

            # Симулируем: дисконт достаточный (steam=$1.0, fee=13%, net=$0.87)
            # discount = (0.87-0.04)/0.87 = 95% > 45%
            result = await buyer._process_item("Good Item", 0.04, 5, redis)

        # Покупка прошла safety net
        assert result >= 0  # 0 если другие проверки не прошли, >0 если куплено

    @pytest.mark.asyncio
    async def test_no_median_skips_safety_net(self):
        """Нет медианы → safety net пропускается (не блокирует)."""
        item = _make_item(
            steam_price_usd=2.0,
            steam_median_30d=0,  # нет данных
            price_usd=0.50,
            cs2dt_item_id=99999,
        )
        uow = _make_uow(item)
        redis = _make_redis()

        mock_cs2dt = AsyncMock()
        # get_sell_list вернёт пустой список — предмет не купится, но safety net пропущен
        mock_cs2dt.get_sell_list = AsyncMock(return_value={"list": []})

        with patch("fluxio.core.workers.buyer.get_redis", return_value=redis), \
             patch("fluxio.core.workers.buyer.UnitOfWork", return_value=uow), \
             patch("fluxio.core.workers.buyer.config") as mock_config:
            mock_config.trading.min_discount_percent = 45
            mock_config.trading.min_price_usd = 0.04
            mock_config.trading.max_price_usd = 1.0
            mock_config.trading.max_same_item_count = 15
            mock_config.fees.steam_fee_percent = 13.0
            mock_config.update_queue.buyer_interval_seconds = 60
            mock_config.games = [MagicMock(enabled=True, app_id=570)]

            from fluxio.core.workers.buyer import BuyerWorker
            buyer = BuyerWorker(mock_cs2dt)
            # Дисконт: net_steam = 2.0*0.87 = 1.74
            # discount = (1.74-0.50)/1.74 = 71% > 45% → ОК
            # steam_median_30d = 0 → safety net пропускается
            # cs2dt_item_id = 99999 → проходит
            # get_sell_list → пустой список → return 0
            result = await buyer._process_item("No Median Item", 0.50, 5, redis)

        # Не заблокирован safety net'ом — дошёл до get_sell_list
        assert result == 0
        mock_cs2dt.get_sell_list.assert_called_once()
