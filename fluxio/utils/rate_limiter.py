"""Rate limiter на основе алгоритма Token Bucket."""

from __future__ import annotations

import asyncio
import time

from loguru import logger


class TokenBucketRateLimiter:
    """Асинхронный rate limiter с алгоритмом Token Bucket.

    Args:
        rate: максимум запросов в секунду.
        burst: максимальный размер всплеска (bucket capacity).
        name: имя лимитера для логирования.
    """

    def __init__(self, rate: float, burst: int | None = None, name: str = "default") -> None:
        self.rate = rate
        self.burst = burst or int(rate)
        self.name = name
        self._tokens: float = float(self.burst)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Пополнить токены на основе прошедшего времени."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.rate
        self._tokens = min(self._tokens + new_tokens, float(self.burst))
        self._last_refill = now

    @property
    def available_tokens(self) -> float:
        """Текущее количество доступных токенов (без блокировки)."""
        return self._tokens

    async def acquire(self) -> None:
        """Дождаться доступного токена и забрать его."""
        async with self._lock:
            self._refill()

            if self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) / self.rate
                logger.debug(
                    f"[{self.name}] Rate limit — ожидание {wait_time:.2f}с"
                )
                await asyncio.sleep(wait_time)
                self._refill()

            self._tokens -= 1.0

            # Предупреждение при 80% загрузке (debug чтобы не спамить)
            usage_percent = (1.0 - self._tokens / self.burst) * 100
            if usage_percent >= 80:
                logger.debug(
                    f"[{self.name}] Высокая загрузка rate limiter: "
                    f"{usage_percent:.0f}% ({self._tokens:.1f}/{self.burst} токенов)"
                )

    async def __aenter__(self) -> TokenBucketRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


class RetryConfig:
    """Конфигурация повторных попыток.

    Args:
        max_retries: максимум повторных попыток.
        base_delay: начальная задержка (секунды).
        max_delay: максимальная задержка (секунды).
        backoff_factor: множитель для экспоненциального отступа.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 16.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor

    def get_delay(self, attempt: int) -> float:
        """Вычислить задержку для данной попытки."""
        delay = self.base_delay * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay)


# Предустановленные конфигурации retry из SPEC.md
RETRY_429 = RetryConfig(max_retries=5, base_delay=1.0, max_delay=16.0, backoff_factor=2.0)
RETRY_5XX = RetryConfig(max_retries=3, base_delay=5.0, max_delay=20.0, backoff_factor=2.0)
