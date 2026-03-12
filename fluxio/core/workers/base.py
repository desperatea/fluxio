"""Базовый класс asyncio-воркера с управлением состоянием и graceful shutdown."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger


class WorkerStatus:
    """Состояние воркера для дашборда и pub/sub."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.running: bool = False
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None
        self.cycles: int = 0
        self.items_processed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать для API/дашборда."""
        return {
            "name": self.name,
            "running": self.running,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "cycles": self.cycles,
            "items_processed": self.items_processed,
        }


class BaseWorker:
    """Базовый asyncio-воркер с бесконечным циклом и graceful shutdown.

    Подклассы реализуют метод run_cycle().
    Воркер запускается через asyncio.create_task(worker.run()).
    """

    def __init__(self, name: str, interval_seconds: int) -> None:
        self._status = WorkerStatus(name)
        self._interval = interval_seconds

    @property
    def status(self) -> WorkerStatus:
        return self._status

    @property
    def name(self) -> str:
        return self._status.name

    async def run_cycle(self) -> None:
        """Один рабочий цикл — реализуется подклассом."""
        raise NotImplementedError

    async def run(self) -> None:
        """Бесконечный цикл воркера с обработкой ошибок."""
        self._status.running = True
        logger.info(f"Воркер [{self._status.name}] запущен (интервал: {self._interval}с)")

        while self._status.running:
            try:
                await self.run_cycle()
                self._status.last_run_at = datetime.now(timezone.utc)
                self._status.cycles += 1
                self._status.last_error = None
                logger.debug(
                    f"Воркер [{self._status.name}] цикл #{self._status.cycles} завершён"
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._status.last_error = str(e)
                logger.error(f"Воркер [{self._status.name}] ошибка цикла: {e}")

            # Пауза между циклами с поддержкой отмены
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

        self._status.running = False
        logger.info(f"Воркер [{self._status.name}] остановлен")

    def stop(self) -> None:
        """Запросить остановку воркера."""
        self._status.running = False
