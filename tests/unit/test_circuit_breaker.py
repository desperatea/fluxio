"""Тесты для Circuit Breaker."""

from __future__ import annotations

import time

import pytest

from fluxio.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
)


def test_initial_state():
    """Начальное состояние — CLOSED."""
    cb = CircuitBreaker(name="test", threshold=3, timeout=10)
    assert cb.state == CircuitState.CLOSED
    assert cb.is_available
    assert cb.failure_count == 0


def test_success_resets_failure_count():
    """Успешный запрос сбрасывает счётчик ошибок."""
    cb = CircuitBreaker(name="test", threshold=5, timeout=10)
    cb.record_failure()
    cb.record_failure()
    assert cb.failure_count == 2
    cb.record_success()
    assert cb.failure_count == 0


def test_opens_after_threshold():
    """После N ошибок подряд — переход в OPEN."""
    cb = CircuitBreaker(name="test", threshold=3, timeout=60)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.is_available


def test_check_raises_when_open():
    """check() бросает CircuitBreakerError в состоянии OPEN."""
    cb = CircuitBreaker(name="test", threshold=1, timeout=60)
    cb.record_failure()
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.check()
    assert "test" in str(exc_info.value)


def test_half_open_after_timeout():
    """Переход OPEN → HALF_OPEN после истечения таймаута."""
    cb = CircuitBreaker(name="test", threshold=1, timeout=0.1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Ждём таймаут
    time.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.is_available


def test_half_open_success_closes():
    """Успешный запрос в HALF_OPEN → возврат в CLOSED."""
    cb = CircuitBreaker(name="test", threshold=1, timeout=0.1)
    cb.record_failure()
    time.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_half_open_failure_reopens():
    """Ошибка в HALF_OPEN → обратно в OPEN."""
    cb = CircuitBreaker(name="test", threshold=1, timeout=0.1)
    cb.record_failure()
    time.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_reset():
    """Принудительный сброс возвращает в CLOSED."""
    cb = CircuitBreaker(name="test", threshold=1, timeout=60)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.is_available


def test_status_dict():
    """status() возвращает словарь с полной информацией."""
    cb = CircuitBreaker(name="my_api", threshold=5, timeout=300)
    cb.record_success()
    cb.record_failure()

    status = cb.status()
    assert status["name"] == "my_api"
    assert status["state"] == "closed"
    assert status["threshold"] == 5
    assert status["total_calls"] == 2
    assert status["total_failures"] == 1
