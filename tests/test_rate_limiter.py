"""Тесты Token Bucket rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from fluxio.utils.rate_limiter import RetryConfig, TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_basic() -> None:
    """Rate limiter должен пропускать запросы в рамках лимита."""
    limiter = TokenBucketRateLimiter(rate=10.0, burst=10, name="test")

    # 10 запросов подряд должны пройти без задержки
    start = time.monotonic()
    for _ in range(10):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    # Должно занять менее 0.5с (запас для CI)
    assert elapsed < 0.5, f"10 запросов заняли {elapsed:.2f}с — слишком долго"


@pytest.mark.asyncio
async def test_rate_limiter_throttle() -> None:
    """Rate limiter должен задерживать при исчерпании токенов."""
    limiter = TokenBucketRateLimiter(rate=5.0, burst=2, name="test-throttle")

    # Первые 2 запроса — мгновенно (burst)
    await limiter.acquire()
    await limiter.acquire()

    # Третий — должен подождать ~0.2с (1/5)
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1, f"Задержка {elapsed:.3f}с — лимитер не тормозит"


@pytest.mark.asyncio
async def test_rate_limiter_context_manager() -> None:
    """Rate limiter должен работать как async context manager."""
    limiter = TokenBucketRateLimiter(rate=10.0, burst=10, name="test-ctx")

    async with limiter:
        pass  # Просто проверяем что не падает


def test_retry_config_delays() -> None:
    """RetryConfig должен корректно вычислять задержки."""
    retry = RetryConfig(base_delay=1.0, backoff_factor=2.0, max_delay=16.0)

    assert retry.get_delay(0) == 1.0
    assert retry.get_delay(1) == 2.0
    assert retry.get_delay(2) == 4.0
    assert retry.get_delay(3) == 8.0
    assert retry.get_delay(4) == 16.0
    assert retry.get_delay(5) == 16.0  # Не превышает max_delay
