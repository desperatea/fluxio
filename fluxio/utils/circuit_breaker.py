"""Circuit Breaker — защита от каскадных отказов внешних API."""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Callable, Coroutine

from loguru import logger


class CircuitState(Enum):
    """Состояние Circuit Breaker."""

    CLOSED = "closed"       # Работает нормально
    OPEN = "open"           # Отключён, все запросы отклоняются
    HALF_OPEN = "half_open" # Пробный запрос для проверки


class CircuitBreakerError(Exception):
    """API отключён circuit breaker'ом."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker '{name}' открыт. "
            f"Повторить через {retry_after:.0f}с"
        )


class CircuitBreaker:
    """Circuit Breaker для внешнего API.

    Отслеживает ошибки подряд. При превышении порога —
    отключает API на заданное время, затем пробует восстановить.

    Args:
        name: Имя API (для логов).
        threshold: Количество ошибок подряд до открытия.
        timeout: Секунд паузы в состоянии OPEN.
    """

    def __init__(
        self,
        name: str,
        threshold: int = 5,
        timeout: float = 300.0,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.timeout = timeout

        self._state = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._success_count: int = 0
        self._total_calls: int = 0
        self._total_failures: int = 0

    @property
    def state(self) -> CircuitState:
        """Текущее состояние с учётом таймаута."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    f"Circuit breaker '{self.name}': OPEN → HALF_OPEN "
                    f"(прошло {elapsed:.0f}с)"
                )
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def is_available(self) -> bool:
        """Можно ли отправлять запросы."""
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        """Зафиксировать успешный запрос."""
        self._total_calls += 1
        self._success_count += 1

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info(f"Circuit breaker '{self.name}': HALF_OPEN → CLOSED (восстановлен)")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Зафиксировать ошибку."""
        self._total_calls += 1
        self._total_failures += 1
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(
                f"Circuit breaker '{self.name}': HALF_OPEN → OPEN "
                f"(пробный запрос провалился)"
            )
        elif self._failure_count >= self.threshold:
            self._state = CircuitState.OPEN
            logger.error(
                f"Circuit breaker '{self.name}': CLOSED → OPEN "
                f"({self._failure_count} ошибок подряд)"
            )

    def check(self) -> None:
        """Проверить доступность. Бросает CircuitBreakerError если OPEN."""
        if not self.is_available:
            elapsed = time.monotonic() - self._last_failure_time
            retry_after = max(0, self.timeout - elapsed)
            raise CircuitBreakerError(self.name, retry_after)

    def reset(self) -> None:
        """Принудительный сброс."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        logger.info(f"Circuit breaker '{self.name}': сброшен вручную")

    def status(self) -> dict[str, Any]:
        """Статус для мониторинга."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "threshold": self.threshold,
            "timeout_seconds": self.timeout,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
        }
