"""Скрипт сканирования рынка C5Game Dota 2.

Запуск:
    python scripts/scan_market.py

Получает все лоты Dota 2, фильтрует по цене из config.yaml,
сохраняет предметы и подходящие ордера в PostgreSQL.
"""

import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from fluxio.utils.logger import setup_logging


async def main() -> None:
    setup_logging("DEBUG")
    logger.info("=== Скан рынка C5Game Dota 2 ===")

    from fluxio.db import repository as repo

    # Создание таблиц
    from fluxio.db.models import Base
    async with repo.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Таблицы БД созданы (PostgreSQL)")

    from fluxio.api.c5game_client import C5GameClient
    from fluxio.core.scanner import MarketScanner

    c5 = C5GameClient()
    try:
        # Баланс для проверки подключения
        balance = await c5.get_balance()
        logger.info(f"Баланс: {balance.get('balance', '?')} CNY")

        # Скан рынка
        scanner = MarketScanner(c5)
        result = await scanner.scan()

        # Вывод сохранённых данных
        async with repo.async_session() as session:
            items = await repo.get_all_items(session)
            logger.info(f"\n{'='*60}")
            logger.info(f"Предметов в каталоге: {len(items)}")
            for item in items:
                logger.info(
                    f"  {item.market_hash_name}: "
                    f"мин.цена={item.min_price_cny} CNY, "
                    f"ордеров={item.listings_count}, "
                    f"герой={item.hero}"
                )

            listings = await repo.get_active_listings(session)
            logger.info(f"\nАктивных ордеров (подходящих по цене): {len(listings)}")
            for lst in listings:
                logger.info(
                    f"  [{lst.c5_id}] {lst.market_hash_name}: "
                    f"{lst.price_cny} CNY, "
                    f"доставка={lst.delivery}"
                )

    finally:
        await c5.close()


if __name__ == "__main__":
    asyncio.run(main())
