"""Telegram бот для уведомлений и команд.

Заглушка для Фазы 1 — полная реализация в Фазе 4.
"""

from __future__ import annotations

from loguru import logger


class TelegramNotifier:
    """Telegram уведомления и команды бота.

    TODO: Реализовать в Фазе 4.
    """

    async def send_message(self, text: str) -> None:
        """Отправить сообщение в Telegram."""
        logger.debug(f"Telegram заглушка: {text[:100]}")

    async def start(self) -> None:
        """Запустить Telegram бота."""
        logger.info("Telegram бот — заглушка Фазы 1")

    async def stop(self) -> None:
        """Остановить Telegram бота."""
        pass
