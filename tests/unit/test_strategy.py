"""Тесты для DiscountStrategy — Фаза 3.

Тестирует:
- Высокий дисконт → should_buy=True
- Низкий дисконт → should_buy=False
- Нулевая Steam цена → отклонение
- Цена вне диапазона → отклонение
- Низкая ликвидность → отклонение
- Предмет в blacklist → отклонение
- Расчёт confidence
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from fluxio.core.strategies.discount import DiscountStrategy
from fluxio.interfaces.market_client import MarketItem
from fluxio.interfaces.price_provider import PriceData


def _make_config(
    min_discount: float = 15.0,
    min_price: float = 0.10,
    max_price: float = 5.0,
    steam_fee: float = 13.0,
    min_sales_7d: int = 10,
    blacklist: list[str] | None = None,
) -> MagicMock:
    """Создать мок конфига."""
    cfg = MagicMock()
    cfg.trading.min_discount_percent = min_discount
    cfg.trading.min_price_usd = min_price
    cfg.trading.max_price_usd = max_price
    cfg.trading.min_sales_volume_7d = min_sales_7d
    cfg.fees.steam_fee_percent = steam_fee
    cfg.blacklist.items = blacklist or []
    return cfg


def _make_item(price: float = 1.0, name: str = "Test Item") -> MarketItem:
    return MarketItem(
        item_id="123",
        product_id="456",
        market_hash_name=name,
        item_name=name,
        price=Decimal(str(price)),
        platform="cs2dt",
    )


def _make_reference(
    median: float = 2.0,
    sales_7d: int = 50,
    name: str = "Test Item",
) -> PriceData:
    return PriceData(
        market_hash_name=name,
        median_price_usd=median,
        lowest_price_usd=None,
        volume_24h=None,
        buy_order_price_usd=None,
        sales_7d=sales_7d,
        updated_at="2026-01-01T00:00:00Z",
    )


class TestDiscountStrategy:
    """Тесты стратегии дисконта."""

    def test_high_discount_should_buy(self):
        """Высокий дисконт → should_buy=True."""
        # Steam $2.0, CS2DT $1.0, fee 13%
        # net = 2.0 * 0.87 = 1.74
        # discount = (1.74 - 1.0) / 1.74 = 42.5%
        strategy = DiscountStrategy(_make_config())
        result = strategy.analyze(_make_item(1.0), _make_reference(2.0))

        assert result.should_buy is True
        assert result.discount_percent == pytest.approx(42.53, rel=0.01)
        assert result.expected_profit_usd > 0
        assert "discount" in result.tags

    def test_low_discount_should_not_buy(self):
        """Низкий дисконт → should_buy=False."""
        # Steam $1.2, CS2DT $1.0
        # net = 1.2 * 0.87 = 1.044
        # discount = (1.044 - 1.0) / 1.044 = 4.2%
        strategy = DiscountStrategy(_make_config())
        result = strategy.analyze(_make_item(1.0), _make_reference(1.2))

        assert result.should_buy is False
        assert "4.2%" in result.reason or "Дисконт" in result.reason

    def test_negative_discount_should_not_buy(self):
        """Отрицательный дисконт (CS2DT дороже Steam) → should_buy=False."""
        strategy = DiscountStrategy(_make_config())
        result = strategy.analyze(_make_item(2.0), _make_reference(1.5))

        assert result.should_buy is False

    def test_zero_steam_price_rejected(self):
        """Steam медиана = 0 → отклонение."""
        strategy = DiscountStrategy(_make_config())
        result = strategy.analyze(_make_item(1.0), _make_reference(0.0))

        assert result.should_buy is False
        assert "Steam медиана" in result.reason

    def test_price_below_range_rejected(self):
        """Цена ниже min_price → отклонение."""
        strategy = DiscountStrategy(_make_config(min_price=0.50))
        result = strategy.analyze(_make_item(0.05), _make_reference(2.0))

        assert result.should_buy is False
        assert "вне диапазона" in result.reason

    def test_price_above_range_rejected(self):
        """Цена выше max_price → отклонение."""
        strategy = DiscountStrategy(_make_config(max_price=3.0))
        result = strategy.analyze(_make_item(4.0), _make_reference(10.0))

        assert result.should_buy is False
        assert "вне диапазона" in result.reason

    def test_low_liquidity_rejected(self):
        """Низкая ликвидность → отклонение."""
        strategy = DiscountStrategy(_make_config(min_sales_7d=100))
        result = strategy.analyze(_make_item(1.0), _make_reference(2.0, sales_7d=5))

        assert result.should_buy is False
        assert "ликвидность" in result.reason.lower()

    def test_blacklisted_item_rejected(self):
        """Предмет в blacklist → отклонение."""
        strategy = DiscountStrategy(_make_config(blacklist=["Blocked Item"]))
        item = _make_item(1.0, name="Blocked Item")
        ref = _make_reference(2.0, name="Blocked Item")
        result = strategy.analyze(item, ref)

        assert result.should_buy is False
        assert "blacklist" in result.reason

    def test_confidence_calculation(self):
        """Confidence рассчитывается на основе дисконта и ликвидности."""
        strategy = DiscountStrategy(_make_config())
        result = strategy.analyze(_make_item(1.0), _make_reference(2.0, sales_7d=50))

        assert result.should_buy is True
        assert 0 < result.confidence <= 1.0

    def test_exact_threshold_should_buy(self):
        """Дисконт ровно = min_discount → should_buy=True."""
        # Подберём цену чтобы дисконт был ровно 15%
        # net = steam * 0.87, discount = (net - price) / net * 100 = 15
        # price = net * 0.85 = steam * 0.87 * 0.85
        steam = 2.0
        price = steam * 0.87 * 0.85  # = 1.479
        strategy = DiscountStrategy(_make_config(min_discount=15.0))
        result = strategy.analyze(_make_item(price), _make_reference(steam))

        assert result.should_buy is True
        assert result.discount_percent == pytest.approx(15.0, abs=0.1)

    def test_strategy_name(self):
        """Имя стратегии = 'discount'."""
        strategy = DiscountStrategy(_make_config())
        assert strategy.name == "discount"

    def test_none_sales_7d_rejected(self):
        """sales_7d=None → ликвидность 0 → отклонение."""
        strategy = DiscountStrategy(_make_config())
        result = strategy.analyze(_make_item(1.0), _make_reference(2.0, sales_7d=0))

        assert result.should_buy is False
