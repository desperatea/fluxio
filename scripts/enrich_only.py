"""Обогащение кандидатов данными Steam (шаги 1-4, без покупки).

Запускает полный цикл анализа:
1. Фильтр из БД steam_items
2. Сравнение с CS2DT (свежие цены)
3. Обогащение Steam (priceoverview + histogram + pricehistory)
4. Верификация кандидатов

НЕ покупает (шаг 5 пропущен).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helper import async_session, init_db
from fluxio.db.models import ArbitragePurchase

from scripts.arbitrage_buy import (
    step1_filter_items,
    step2_compare_cs2dt,
    step3_enrich_steam,
    step4_verify,
    MAX_QTY_PER_ITEM,
)

from fluxio.api.cs2dt_client import CS2DTClient
from fluxio.api.steam_client import SteamClient
from fluxio.utils.logger import setup_logging
from loguru import logger


async def main() -> None:
    setup_logging("INFO")
    logger.info("=" * 60)
    logger.info("  ОБОГАЩЕНИЕ КАНДИДАТОВ (без покупки)")
    logger.info("=" * 60)

    await init_db()

    cs2dt = CS2DTClient()
    steam = SteamClient()

    try:
        async with async_session() as session:
            # Шаг 1
            steam_items = await step1_filter_items(session)
            if not steam_items:
                logger.error("Нет подходящих предметов в БД")
                return

            # Уже купленные
            already_bought_counts: dict[str, int] = {}
            try:
                result = await session.execute(
                    select(ArbitragePurchase.hash_name, func.count())
                    .where(ArbitragePurchase.purchased_at >= text("(NOW() - INTERVAL '7 days')::text"))
                    .group_by(ArbitragePurchase.hash_name)
                )
                already_bought_counts = {r[0]: r[1] for r in result.all()}
            except Exception:
                pass

            already_purchased = {
                name for name, count in already_bought_counts.items()
                if count >= MAX_QTY_PER_ITEM
            }

            total_prev = sum(already_bought_counts.values())
            logger.info(f"Куплено за 7 дней: {total_prev} шт ({len(already_bought_counts)} уникальных)")

            # Шаг 2
            candidates = await step2_compare_cs2dt(cs2dt, steam_items, already_purchased)
            if not candidates:
                logger.info("Нет кандидатов с достаточной прибылью")
                return

            # Шаг 3 — обогащение Steam (долгий шаг)
            await step3_enrich_steam(steam, session, candidates)

            # Шаг 4 — верификация
            verified = await step4_verify(session, candidates, already_bought_counts)

            if not verified:
                logger.info("Ни один кандидат не прошёл верификацию")
            else:
                total_spend = sum(c.buy_quantity * c.cs2dt_price for c in verified)
                total_profit = sum(
                    c.buy_quantity * (c.steam_price * 0.85 - c.cs2dt_price)
                    for c in verified
                )
                logger.info("=" * 60)
                logger.info("  ИТОГО (без покупки)")
                logger.info(f"  Верифицированных: {len(verified)} предметов")
                logger.info(f"  Штук к покупке:   {sum(c.buy_quantity for c in verified)}")
                logger.info(f"  Сумма:            ${total_spend:.2f}")
                logger.info(f"  Ожид. прибыль:    +${total_profit:.2f}")
                if total_spend > 0:
                    logger.info(f"  ROI:              {total_profit/total_spend*100:.1f}%")
                logger.info("=" * 60)

        # Шаг 5 ПРОПУЩЕН — покупка не выполняется
        logger.info("Шаг 5 (покупка) ПРОПУЩЕН — только обогащение")

    finally:
        await steam.close()
        await cs2dt.close()

    logger.info("Обогащение завершено")


if __name__ == "__main__":
    asyncio.run(main())
