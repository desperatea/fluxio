"""Настройка логирования через Loguru + SSE буфер."""

from __future__ import annotations

import asyncio
import os
import sys
from collections import deque
from typing import Any

from loguru import logger

# Формат логов
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{line} | {message}"

# Параметры ротации
LOG_DIR = "logs"
LOG_ROTATION = "24 hours"
LOG_RETENTION = 30
LOG_MAX_SIZE = "100 MB"
LOG_COMPRESSION = "gz"

# Буфер логов для SSE стриминга
_log_buffer: deque[str] = deque(maxlen=500)
_log_subscribers: list[asyncio.Queue[str]] = []


def _sse_sink(message: Any) -> None:
    """Sink для Loguru — складывает логи в буфер и рассылает подписчикам SSE."""
    formatted = message.rstrip("\n") if isinstance(message, str) else str(message).rstrip("\n")
    _log_buffer.append(formatted)
    for q in _log_subscribers:
        try:
            q.put_nowait(formatted)
        except asyncio.QueueFull:
            pass


def get_log_buffer() -> list[str]:
    """Получить последние N логов из буфера."""
    return list(_log_buffer)


def subscribe_logs() -> asyncio.Queue[str]:
    """Подписаться на realtime логи (для SSE)."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
    _log_subscribers.append(q)
    return q


def unsubscribe_logs(q: asyncio.Queue[str]) -> None:
    """Отписаться от realtime логов."""
    if q in _log_subscribers:
        _log_subscribers.remove(q)


def setup_logging(level: str | None = None) -> None:
    """Настроить логирование для приложения."""
    log_level = level or os.getenv("LOG_LEVEL", "INFO")

    logger.remove()

    # Консольный вывод
    logger.add(
        sys.stderr,
        format=LOG_FORMAT,
        level=log_level,
        colorize=True,
    )

    # Файловый вывод с ротацией
    logger.add(
        f"{LOG_DIR}/bot_{{time:YYYY-MM-DD}}.log",
        format=LOG_FORMAT,
        level=log_level,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression=LOG_COMPRESSION,
        encoding="utf-8",
    )

    # Отдельный файл для ошибок
    logger.add(
        f"{LOG_DIR}/errors_{{time:YYYY-MM-DD}}.log",
        format=LOG_FORMAT,
        level="ERROR",
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression=LOG_COMPRESSION,
        encoding="utf-8",
    )

    # SSE буфер — ловит все логи для стриминга в дашборд
    logger.add(
        _sse_sink,
        format=LOG_FORMAT,
        level=log_level,
    )

    logger.info(f"Логирование настроено, уровень: {log_level}")
