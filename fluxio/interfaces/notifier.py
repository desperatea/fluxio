"""Protocol для уведомлений."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable


class EventType(Enum):
    """Типы событий для уведомлений."""

    PURCHASE_SUCCESS = "purchase_success"
    PURCHASE_ERROR = "purchase_error"
    LOW_BALANCE = "low_balance"
    DAILY_LIMIT = "daily_limit_reached"
    API_DOWN = "api_unavailable"
    GOOD_DEAL = "good_deal_found"
    CIRCUIT_OPEN = "circuit_breaker_open"
    KILL_SWITCH = "kill_switch_triggered"
    DAILY_REPORT = "daily_report"


@runtime_checkable
class Notifier(Protocol):
    """Протокол отправки уведомлений.

    Основная реализация — Telegram.
    """

    async def notify(
        self,
        event: EventType,
        message: str,
        *,
        urgent: bool = False,
    ) -> bool:
        """Отправить уведомление.

        Args:
            event: Тип события.
            message: Текст уведомления.
            urgent: Срочное — игнорирует quiet_mode.

        Returns:
            True если доставлено успешно.
        """
        ...

    async def send_daily_report(self, report: dict[str, Any]) -> bool:
        """Отправить ежедневный отчёт."""
        ...

    async def close(self) -> None:
        """Закрыть ресурсы."""
        ...
