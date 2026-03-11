"""Экспорт данных в CSV/Excel.

Заглушка для Фазы 1 — полная реализация в Фазе 5.
"""

from __future__ import annotations

from loguru import logger


async def export_purchases_csv(days: int = 30) -> str | None:
    """Экспортировать покупки в CSV.

    TODO: Реализовать в Фазе 5.
    """
    logger.debug("Экспорт CSV — заглушка Фазы 1")
    return None
