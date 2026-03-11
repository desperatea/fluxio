"""Protocol для стратегий арбитража."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fluxio.interfaces.market_client import MarketItem
from fluxio.interfaces.price_provider import PriceData


@dataclass
class AnalysisResult:
    """Результат анализа предмета стратегией."""

    should_buy: bool
    reason: str
    item: MarketItem
    discount_percent: float = 0.0
    expected_profit_usd: float = 0.0
    confidence: float = 0.0        # 0.0–1.0, уверенность в сделке
    tags: list[str] = field(default_factory=list)


@runtime_checkable
class Strategy(Protocol):
    """Протокол арбитражной стратегии."""

    @property
    def name(self) -> str:
        """Уникальное имя стратегии для логирования."""
        ...

    def analyze(
        self,
        item: MarketItem,
        reference: PriceData,
    ) -> AnalysisResult:
        """Проанализировать предмет и принять решение."""
        ...
