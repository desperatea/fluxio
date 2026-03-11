"""Protocol для провайдеров эталонных цен."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PriceData:
    """Эталонная цена предмета."""

    market_hash_name: str
    median_price_usd: float         # Медиана продаж (основной ориентир)
    lowest_price_usd: float | None  # Минимальная цена листинга
    volume_24h: int | None          # Объём продаж за 24ч
    buy_order_price_usd: float | None  # Лучший buy order
    sales_7d: int | None            # Продаж за 7 дней (ликвидность)
    updated_at: str                 # ISO timestamp последнего обновления


@runtime_checkable
class PriceProvider(Protocol):
    """Протокол для источника эталонных цен.

    Основная реализация — Steam Market.
    """

    async def get_price(
        self,
        market_hash_name: str,
        app_id: int = 570,
    ) -> PriceData | None:
        """Получить актуальную цену предмета.

        Returns:
            PriceData или None если предмет не найден.
        """
        ...

    async def get_prices_batch(
        self,
        names: list[str],
        app_id: int = 570,
    ) -> dict[str, PriceData]:
        """Пакетное получение цен (для оптимизации)."""
        ...

    async def get_sales_history(
        self,
        market_hash_name: str,
        app_id: int = 570,
        days: int = 30,
    ) -> list[tuple[str, float, int]]:
        """История продаж: [(date, price, volume), ...]."""
        ...

    async def close(self) -> None:
        """Закрыть ресурсы."""
        ...
