"""Настройка логирования через Loguru."""

from __future__ import annotations

import os
import sys

from loguru import logger

# Формат логов из SPEC.md раздел 11
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{line} | {message}"

# Параметры ротации
LOG_DIR = "logs"
LOG_ROTATION = "24 hours"
LOG_RETENTION = 30  # файлов
LOG_MAX_SIZE = "100 MB"
LOG_COMPRESSION = "gz"


def setup_logging(level: str | None = None) -> None:
    """Настроить логирование для приложения.

    Args:
        level: уровень логирования (INFO, DEBUG и т.д.).
               Если не указан, берётся из LOG_LEVEL или INFO.
    """
    log_level = level or os.getenv("LOG_LEVEL", "INFO")

    # Убираем стандартный обработчик
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

    logger.info(f"Логирование настроено, уровень: {log_level}")
