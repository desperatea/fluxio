"""Сравнение цен Steam Market vs CS2DT для арбитража.

Берёт просканированные предметы из steam_items (PostgreSQL),
запрашивает цены на CS2DT пакетно, считает прибыль с учётом комиссии Steam.

Запуск:
    python scripts/compare_prices.py [--min-price 0.10] [--min-profit 0.01]
    python scripts/compare_prices.py --liquid              # только ликвидные
    python scripts/compare_prices.py --csv results.csv     # экспорт в CSV
"""

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass, fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db_helper import async_session, init_db
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.db.models import SteamItem
from fluxio.utils.logger import setup_logging

# Комиссия Steam для Dota 2: 5% Steam + 10% Valve = 15%
STEAM_FEE_RATE = 0.15
# После комиссии продавец получает 85%
STEAM_SELLER_RECEIVES = 1.0 - STEAM_FEE_RATE


@dataclass
class ArbitrageItem:
    """Результат арбитражного анализа предмета."""
    hash_name: str
    steam_price: float       # Минимальная цена продажи на Steam
    cs2dt_price: float       # Минимальная цена на CS2DT
    steam_after_fee: float   # Steam цена после комиссии
    profit: float            # Прибыль при покупке на CS2DT, продаже на Steam
    profit_pct: float        # Прибыль в процентах
    steam_volume: int        # Кол-во лотов на Steam
    cs2dt_quantity: int      # Кол-во лотов на CS2DT


async def main(args: argparse.Namespace) -> None:
    setup_logging("INFO")
    logger.info("=== Сравнение цен Steam vs CS2DT (Dota 2) ===")

    await init_db()

    # 1. Загрузить предметы из PostgreSQL
    async with async_session() as session:
        result = await session.execute(
            select(SteamItem.hash_name, SteamItem.sell_price_usd, SteamItem.sell_listings)
            .where(SteamItem.sell_price_usd >= args.min_price)
            .order_by(SteamItem.sell_price_usd.desc())
        )
        steam_items = result.all()

    logger.info(f"Предметов из Steam с ценой >= ${args.min_price:.2f}: {len(steam_items)}")

    if not steam_items:
        logger.warning("Нет предметов для сравнения. Сначала запустите scan_steam_dota2.py")
        return

    # 2. Запросить цены на CS2DT пакетно (по 200 штук)
    cs2dt = CS2DTClient()
    cs2dt_prices: dict[str, dict] = {}

    hash_names = [item[0] for item in steam_items]
    batch_size = 200

    try:
        for i in range(0, len(hash_names), batch_size):
            batch = hash_names[i:i + batch_size]
            logger.info(f"Запрос CS2DT цен: {i+1}–{i+len(batch)} из {len(hash_names)}")

            try:
                result = await cs2dt.get_prices_batch(batch, app_id=570)
                if isinstance(result, list):
                    for item_data in result:
                        name = item_data.get("marketHashName", "")
                        if name:
                            cs2dt_prices[name] = item_data
                logger.info(f"  Получено цен: {len(result) if isinstance(result, list) else 0}")
            except CS2DTAPIError as e:
                logger.error(f"  Ошибка CS2DT: {e}")
                continue

    finally:
        await cs2dt.close()

    logger.info(f"CS2DT вернул цены для {len(cs2dt_prices)} предметов")

    # 3. Сравнить цены и посчитать прибыль
    arbitrage_items: list[ArbitrageItem] = []

    for hash_name, steam_price, steam_listings in steam_items:
        cs2dt_data = cs2dt_prices.get(hash_name)
        if not cs2dt_data:
            continue

        # Цена на CS2DT (минимальная)
        cs2dt_price_str = cs2dt_data.get("price")
        if not cs2dt_price_str:
            continue
        cs2dt_price = float(cs2dt_price_str)
        if cs2dt_price <= 0:
            continue

        cs2dt_qty = int(cs2dt_data.get("quantity", 0))

        # Прибыль: покупаем на CS2DT, продаём на Steam
        # Продавец на Steam получает: steam_price * 0.85
        steam_after_fee = steam_price * STEAM_SELLER_RECEIVES
        profit = steam_after_fee - cs2dt_price
        profit_pct = (profit / cs2dt_price * 100) if cs2dt_price > 0 else 0

        if profit >= args.min_profit:
            arbitrage_items.append(ArbitrageItem(
                hash_name=hash_name,
                steam_price=steam_price,
                cs2dt_price=cs2dt_price,
                steam_after_fee=round(steam_after_fee, 4),
                profit=round(profit, 4),
                profit_pct=round(profit_pct, 2),
                steam_volume=steam_listings,
                cs2dt_quantity=cs2dt_qty,
            ))

    # 4. Фильтр ликвидности
    if args.liquid:
        before = len(arbitrage_items)
        arbitrage_items = [
            item for item in arbitrage_items
            if item.steam_volume >= args.min_steam_listings
            and item.cs2dt_quantity >= args.min_cs2dt_listings
        ]
        logger.info(
            f"Фильтр ликвидности: Steam >= {args.min_steam_listings} лотов, "
            f"CS2DT >= {args.min_cs2dt_listings} лотов → "
            f"{len(arbitrage_items)} из {before}"
        )

    # 5. Сортировка по прибыли (убывание)
    arbitrage_items.sort(key=lambda x: x.profit_pct, reverse=True)

    # 6. Экспорт в CSV
    if args.csv:
        csv_path = Path(args.csv)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Название", "Прибыль $", "Прибыль %",
                "Цена CS2DT $", "Цена Steam $", "После комиссии $",
                "Лоты CS2DT", "Лоты Steam",
            ])
            for item in arbitrage_items:
                writer.writerow([
                    item.hash_name, f"{item.profit:.2f}", f"{item.profit_pct:.1f}",
                    f"{item.cs2dt_price:.2f}", f"{item.steam_price:.2f}",
                    f"{item.steam_after_fee:.2f}",
                    item.cs2dt_quantity, item.steam_volume,
                ])
        logger.info(f"Результаты экспортированы в {csv_path.resolve()}")

    # 7. Вывод результатов
    total_found = len(arbitrage_items)
    logger.info(f"\n{'='*100}")
    logger.info(f"  Найдено выгодных предметов: {total_found}")
    logger.info(f"  (покупка на CS2DT → продажа на Steam, комиссия Steam 15%)")
    logger.info(f"{'='*100}")

    if not arbitrage_items:
        logger.info("  Выгодных предметов не найдено")
        return

    # Заголовок таблицы
    header = (
        f"{'#':>4}  "
        f"{'Прибыль':>8}  "
        f"{'%':>7}  "
        f"{'CS2DT':>8}  "
        f"{'Steam':>8}  "
        f"{'После ком.':>10}  "
        f"{'Лоты CS2DT':>10}  "
        f"{'Лоты Steam':>10}  "
        f"{'Название'}"
    )
    logger.info(header)
    logger.info("─" * 120)

    for i, item in enumerate(arbitrage_items, 1):
        line = (
            f"{i:>4}  "
            f"${item.profit:>7.2f}  "
            f"{item.profit_pct:>6.1f}%  "
            f"${item.cs2dt_price:>7.2f}  "
            f"${item.steam_price:>7.2f}  "
            f"${item.steam_after_fee:>9.2f}  "
            f"{item.cs2dt_quantity:>10}  "
            f"{item.steam_volume:>10}  "
            f"{item.hash_name[:60]}"
        )
        logger.info(line)

    # Суммарная статистика
    total_potential = sum(item.profit for item in arbitrage_items)
    avg_profit_pct = sum(item.profit_pct for item in arbitrage_items) / len(arbitrage_items)
    logger.info("─" * 120)
    logger.info(f"  Итого предметов: {total_found}")
    logger.info(f"  Суммарная потенциальная прибыль: ${total_potential:.2f}")
    logger.info(f"  Средний % прибыли: {avg_profit_pct:.1f}%")
    logger.info(f"  Топ-10 по абсолютной прибыли:")

    by_abs_profit = sorted(arbitrage_items, key=lambda x: x.profit, reverse=True)[:10]
    for item in by_abs_profit:
        logger.info(
            f"    ${item.profit:>7.2f} ({item.profit_pct:>5.1f}%)  "
            f"CS2DT ${item.cs2dt_price:.2f} → Steam ${item.steam_price:.2f}  "
            f"[CS2DT:{item.cs2dt_quantity} Steam:{item.steam_volume}]  "
            f"{item.hash_name[:50]}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сравнение цен Steam vs CS2DT")
    parser.add_argument("--min-price", type=float, default=0.10, help="Мин. цена Steam USD (по умолч. 0.10)")
    parser.add_argument("--min-profit", type=float, default=0.01, help="Мин. прибыль USD (по умолч. 0.01)")
    parser.add_argument("--liquid", action="store_true", help="Только ликвидные (фильтр по лотам)")
    parser.add_argument("--min-steam-listings", type=int, default=5, help="Мин. лотов на Steam (по умолч. 5)")
    parser.add_argument("--min-cs2dt-listings", type=int, default=2, help="Мин. лотов на CS2DT (по умолч. 2)")
    parser.add_argument("--csv", type=str, default=None, help="Экспорт в CSV файл")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
