"""Логика покупки предметов.

Заглушка для Фазы 1 — полная реализация в Фазе 3.
"""

from __future__ import annotations

from loguru import logger


class Buyer:
    """Модуль покупки: quick → batch → normal.

    TODO: Реализовать в Фазе 3.
    """

    async def buy_item(self, product_id: str, price_cny: float) -> bool:
        """Купить предмет (или симулировать в dry-run).

        Returns:
            True если покупка успешна.
        """
        logger.info("Покупка — заглушка Фазы 1")
        return False
