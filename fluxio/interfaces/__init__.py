"""Protocol интерфейсы Fluxio."""

from fluxio.interfaces.market_client import BuyResult, MarketClient, MarketItem
from fluxio.interfaces.notifier import EventType, Notifier
from fluxio.interfaces.price_provider import PriceData, PriceProvider
from fluxio.interfaces.strategy import AnalysisResult, Strategy

__all__ = [
    "BuyResult",
    "MarketClient",
    "MarketItem",
    "EventType",
    "Notifier",
    "PriceData",
    "PriceProvider",
    "AnalysisResult",
    "Strategy",
]
