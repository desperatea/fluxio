"""Protocol для торговых площадок."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass
class MarketItem:
    """Предмет на торговой площадке."""

    item_id: str                    # ID предмета на площадке (itemId)
    product_id: str                 # ID конкретного листинга (productId)
    market_hash_name: str           # Canonical имя (Steam market_hash_name)
    item_name: str                  # Отображаемое имя
    price: Decimal                  # Цена покупки (USD)
    platform: str                   # Название площадки ('cs2dt', 'c5game')
    app_id: int = 570               # Steam AppID (570=Dota2, 730=CS2)
    quantity: int = 1               # Доступное количество
    auto_deliver: bool = False      # Авто-доставка (не P2P)
    image_url: str = ""             # URL изображения
    raw: dict[str, Any] | None = None  # Исходный ответ API


@dataclass
class BuyResult:
    """Результат покупки."""

    success: bool
    order_id: str | None = None     # ID заказа на площадке
    error: str | None = None        # Сообщение об ошибке
    raw: dict[str, Any] | None = None


@runtime_checkable
class MarketClient(Protocol):
    """Протокол для любой торговой площадки (CS2DT, C5Game, BUFF и др.).

    Каждая площадка реализует этот интерфейс.
    Подключение новой площадки = новый файл с классом,
    реализующим эти методы.
    """

    @property
    def platform_name(self) -> str:
        """Уникальное имя площадки ('cs2dt', 'c5game', 'buff')."""
        ...

    async def search_market(
        self,
        app_id: int,
        page: int = 1,
        page_size: int = 50,
    ) -> list[MarketItem]:
        """Поиск предметов на рынке."""
        ...

    async def get_item_listings(
        self,
        item_id: str,
        page: int = 1,
    ) -> list[MarketItem]:
        """Получить конкретные листинги предмета (ордера на продажу)."""
        ...

    async def get_balance(self) -> Decimal:
        """Текущий баланс аккаунта (USD)."""
        ...

    async def buy(
        self,
        product_id: str,
        price: Decimal,
        *,
        dry_run: bool = True,
    ) -> BuyResult:
        """Купить конкретный листинг."""
        ...

    async def get_order_status(self, order_id: str) -> str:
        """Статус заказа: 'pending' | 'delivered' | 'cancelled' | 'expired'."""
        ...

    async def close(self) -> None:
        """Закрыть HTTP-сессию и освободить ресурсы."""
        ...
