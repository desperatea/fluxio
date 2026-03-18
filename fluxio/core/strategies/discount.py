"""Стратегия дисконта — покупка предметов со скидкой относительно Steam медианы.

Условия покупки (SPEC.md раздел 5.2):
1. Item имеет Steam цену
2. Discount >= min_discount_percent
3. Liquidity >= min_sales_volume_7d
4. Price в [min_price_usd, max_price_usd]
5. Не в blacklist
"""

from __future__ import annotations

from decimal import Decimal

from fluxio.config import AppConfig
from fluxio.interfaces.market_client import MarketItem
from fluxio.interfaces.price_provider import PriceData
from fluxio.interfaces.strategy import AnalysisResult


class DiscountStrategy:
    """Стратегия дисконта: покупка если цена CS2DT значительно ниже Steam медианы."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "discount"

    def analyze(
        self,
        item: MarketItem,
        reference: PriceData,
    ) -> AnalysisResult:
        """Проанализировать предмет и принять решение о покупке."""
        trading = self._config.trading
        fees = self._config.fees
        item_price = float(item.price)

        # Проверка: Steam цена должна быть положительной
        if reference.median_price_usd <= 0:
            return AnalysisResult(
                should_buy=False,
                reason="Steam медиана <= 0",
                item=item,
            )

        # Проверка: цена в допустимом диапазоне
        if not (trading.min_price_usd <= item_price <= trading.max_price_usd):
            return AnalysisResult(
                should_buy=False,
                reason=f"Цена ${item_price:.4f} вне диапазона "
                       f"[${trading.min_price_usd}, ${trading.max_price_usd}]",
                item=item,
            )

        # Проверка: ликвидность
        sales_7d = reference.sales_7d or 0
        if sales_7d < trading.min_sales_volume_7d:
            return AnalysisResult(
                should_buy=False,
                reason=f"Низкая ликвидность: {sales_7d} продаж/7д "
                       f"(мин: {trading.min_sales_volume_7d})",
                item=item,
            )

        # Проверка: blacklist
        if item.market_hash_name in self._config.blacklist.items:
            return AnalysisResult(
                should_buy=False,
                reason="Предмет в blacklist",
                item=item,
            )

        # Расчёт ROI с учётом комиссии Steam (поцентовый расчёт)
        from fluxio.config import FeesConfig
        net_steam = FeesConfig.calc_net_steam(
            reference.median_price_usd,
            fees.steam_valve_fee_percent,
            fees.steam_game_fee_percent,
        )
        discount = (net_steam - item_price) / item_price * 100 if item_price > 0 else 0
        expected_profit = net_steam - item_price

        # Проверка: дисконт >= порога
        if discount < trading.min_discount_percent:
            return AnalysisResult(
                should_buy=False,
                reason=f"Дисконт {discount:.1f}% < {trading.min_discount_percent}%",
                item=item,
                discount_percent=discount,
                expected_profit_usd=expected_profit,
            )

        # Уверенность на основе дисконта и ликвидности
        confidence = min(1.0, (discount / 100) + (min(sales_7d, 100) / 200))

        return AnalysisResult(
            should_buy=True,
            reason=f"Дисконт {discount:.1f}%, прибыль ${expected_profit:.4f}",
            item=item,
            discount_percent=round(discount, 2),
            expected_profit_usd=round(expected_profit, 4),
            confidence=round(confidence, 3),
            tags=["discount"],
        )
