"""Сканер предметов Dota 2 на торговой площадке Steam.

Запуск:
    python scripts/scan_steam_dota2.py [--enrich] [--orders] [--limit N]

Фазы:
  1. Обход всех предметов через search/render API (название, кол-во лотов, мин.цена)
  2. --enrich: обогащение данными priceoverview (медианная цена, объём 24ч)
  3. --orders: получение ордеров на покупку (item_nameid + itemordershistogram)

Данные записываются в PostgreSQL (таблицы steam_items, steam_sales_history).
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# Корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_helper import engine, async_session, init_db
from fluxio.api.steam_client import SteamClient
from fluxio.db.models import SteamItem, SteamSalesHistory
from fluxio.utils.logger import setup_logging

APP_ID = 570  # Dota 2
PAGE_SIZE = 10  # Steam API ограничивает до 10 результатов за запрос


# ─────────────────────────────────────────────────────
# Фаза 1: Обход всех предметов через search/render
# ─────────────────────────────────────────────────────

async def phase1_scan_all_items(
    steam: SteamClient,
    session: AsyncSession,
    limit: int = 0,
    start_offset: int = 0,
) -> int:
    """Собрать все предметы Dota 2 через search/render API.

    Args:
        start_offset: Начать сканирование с указанной позиции (для продолжения).

    Returns:
        Количество сохранённых предметов.
    """
    logger.info("═══ Фаза 1: Сканирование всех предметов Steam Market (Dota 2) ═══")

    # Первый запрос — узнаём total_count
    data = await steam.search_market(app_id=APP_ID, start=start_offset, count=PAGE_SIZE)
    if not data:
        logger.error("Не удалось получить первую страницу — проверьте подключение")
        return 0

    total_count = data.get("total_count", 0)
    logger.info(f"Всего предметов на Steam Market Dota 2: {total_count}")

    if start_offset > 0:
        logger.info(f"Продолжение сканирования с позиции {start_offset}")

    if limit > 0:
        total_count = min(total_count, start_offset + limit)
        logger.info(f"Ограничение: сканируем до позиции {total_count}")

    # Сохраняем первую порцию
    saved = await _save_search_results(session, data.get("results", []))
    start = start_offset + PAGE_SIZE

    while start < total_count:
        data = await steam.search_market(app_id=APP_ID, start=start, count=PAGE_SIZE)
        if not data:
            logger.warning(f"Ошибка на start={start}, пропускаю")
            start += PAGE_SIZE
            continue

        results = data.get("results", [])
        if not results:
            logger.info(f"Пустая страница на start={start}, завершаю обход")
            break

        batch_saved = await _save_search_results(session, results)
        saved += batch_saved
        start += PAGE_SIZE

        # Прогресс
        pct = min(100, start * 100 // total_count)
        logger.info(
            f"Прогресс: {start}/{total_count} ({pct}%), "
            f"сохранено: {saved} предметов"
        )

    logger.info(f"Фаза 1 завершена: {saved} предметов сохранено в БД")
    return saved


async def _save_search_results(
    session: AsyncSession,
    results: list[dict],
) -> int:
    """Сохранить результаты search/render в БД."""
    now = datetime.now(timezone.utc).isoformat()
    saved = 0

    for item in results:
        hash_name = item.get("hash_name", "")
        if not hash_name:
            continue

        name = item.get("name", hash_name)
        sell_listings = item.get("sell_listings", 0)
        sell_price = item.get("sell_price", 0) / 100.0  # центы → доллары
        sell_price_text = item.get("sell_price_text", "")

        # Иконка и тип из asset_description
        asset = item.get("asset_description") or {}
        icon_url = asset.get("icon_url", "")
        item_type = asset.get("type", "")

        # Upsert через PostgreSQL INSERT ... ON CONFLICT
        stmt = pg_insert(SteamItem).values(
            hash_name=hash_name,
            name=name,
            sell_listings=sell_listings,
            sell_price_usd=sell_price,
            sell_price_text=sell_price_text,
            icon_url=icon_url,
            item_type=item_type,
            first_seen_at=now,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=["hash_name"],
            set_={
                "name": name,
                "sell_listings": sell_listings,
                "sell_price_usd": sell_price,
                "sell_price_text": sell_price_text,
                "icon_url": icon_url,
                "item_type": item_type,
                "updated_at": now,
            },
        )
        await session.execute(stmt)
        saved += 1

    await session.commit()
    return saved


# ─────────────────────────────────────────────────────
# Фаза 2: Обогащение priceoverview (медианная цена, объём)
# ─────────────────────────────────────────────────────

async def phase2_enrich_prices(
    steam: SteamClient,
    session: AsyncSession,
    limit: int = 0,
) -> int:
    """Обогатить предметы данными priceoverview.

    Returns:
        Количество обновлённых предметов.
    """
    logger.info("═══ Фаза 2: Обогащение ценами (priceoverview) ═══")

    # Берём предметы без median_price, приоритет: наш ценовой диапазон + ликвидные
    query = (
        select(SteamItem.hash_name)
        .where(SteamItem.median_price_usd.is_(None))
        .order_by(
            # CASE WHEN sell_price_usd BETWEEN 0.05 AND 0.80 THEN 0 ELSE 1 END
            text("CASE WHEN sell_price_usd BETWEEN 0.05 AND 0.80 THEN 0 ELSE 1 END"),
            SteamItem.sell_listings.desc(),
        )
    )
    if limit > 0:
        query = query.limit(limit)

    result = await session.execute(query)
    items = result.scalars().all()
    total = len(items)
    logger.info(f"Предметов для обогащения: {total}")

    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    for i, hash_name in enumerate(items, 1):
        overview = await steam.get_price_overview(hash_name, app_id=APP_ID)
        if not overview:
            logger.debug(f"[{i}/{total}] Нет данных: {hash_name}")
            continue

        median = steam._parse_price_string(overview.get("median_price", ""))
        lowest = steam._parse_price_string(overview.get("lowest_price", ""))
        volume_str = overview.get("volume", "0")
        volume = int(volume_str.replace(",", "")) if volume_str else 0

        stmt = (
            select(SteamItem)
            .where(SteamItem.hash_name == hash_name)
        )
        item_obj = (await session.execute(stmt)).scalar_one_or_none()
        if item_obj:
            item_obj.median_price_usd = median
            item_obj.lowest_price_usd = lowest
            item_obj.volume_24h = volume
            item_obj.updated_at = now
        updated += 1

        if i % 10 == 0:
            await session.commit()
            logger.info(f"Прогресс priceoverview: {i}/{total} ({i*100//total}%), обновлено: {updated}")

    await session.commit()
    logger.info(f"Фаза 2 завершена: {updated}/{total} предметов обогащены ценами")
    return updated


# ─────────────────────────────────────────────────────
# Фаза 3: Ордера на покупку (itemordershistogram)
# ─────────────────────────────────────────────────────

async def phase3_fetch_orders(
    steam: SteamClient,
    session: AsyncSession,
    limit: int = 0,
) -> int:
    """Получить ордера на покупку для предметов.

    Returns:
        Количество обновлённых предметов.
    """
    logger.info("═══ Фаза 3: Получение ордеров на покупку ═══")

    # Предметы без данных по ордерам
    query = (
        select(SteamItem.hash_name, SteamItem.item_nameid)
        .where(SteamItem.buy_order_price_usd.is_(None))
        .order_by(SteamItem.sell_listings.desc())
    )
    if limit > 0:
        query = query.limit(limit)

    result = await session.execute(query)
    items = result.all()
    total = len(items)
    logger.info(f"Предметов для получения ордеров: {total}")

    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    for i, (hash_name, existing_nameid) in enumerate(items, 1):
        # Шаг 1: получить item_nameid (если ещё нет)
        nameid = existing_nameid
        if not nameid:
            nameid = await steam.get_item_nameid(hash_name, app_id=APP_ID)
            if not nameid:
                logger.debug(f"[{i}/{total}] Не удалось получить nameid: {hash_name}")
                continue
            # Сохраняем nameid
            item_obj = (await session.execute(
                select(SteamItem).where(SteamItem.hash_name == hash_name)
            )).scalar_one_or_none()
            if item_obj:
                item_obj.item_nameid = nameid

        # Шаг 2: получить гистограмму ордеров
        histogram = await steam.get_item_orders_histogram(nameid)
        if not histogram:
            logger.debug(f"[{i}/{total}] Нет гистограммы: {hash_name} (nameid={nameid})")
            continue

        # highest_buy_order и lowest_sell_order в центах (строки)
        buy_price_cents = histogram.get("highest_buy_order")
        sell_price_cents = histogram.get("lowest_sell_order")

        buy_price = int(buy_price_cents) / 100.0 if buy_price_cents else None
        sell_price = int(sell_price_cents) / 100.0 if sell_price_cents else None

        # Количество ордеров из графиков
        buy_graph = histogram.get("buy_order_graph") or []
        sell_graph = histogram.get("sell_order_graph") or []
        buy_count = sum(int(entry[1]) for entry in buy_graph) if buy_graph else 0
        sell_count = sum(int(entry[1]) for entry in sell_graph) if sell_graph else 0

        item_obj = (await session.execute(
            select(SteamItem).where(SteamItem.hash_name == hash_name)
        )).scalar_one_or_none()
        if item_obj:
            item_obj.buy_order_price_usd = buy_price
            item_obj.buy_order_count = buy_count
            item_obj.sell_order_price_usd = sell_price
            item_obj.sell_order_count = sell_count
            item_obj.updated_at = now
        updated += 1

        if i % 10 == 0:
            await session.commit()
            logger.info(f"Прогресс ордеров: {i}/{total} ({i*100//total}%), обновлено: {updated}")

    await session.commit()
    logger.info(f"Фаза 3 завершена: {updated}/{total} предметов обогащены ордерами")
    return updated


# ─────────────────────────────────────────────────────
# Фаза 4: История продаж (pricehistory)
# ─────────────────────────────────────────────────────

async def phase4_fetch_history(
    steam: SteamClient,
    session: AsyncSession,
    limit: int = 0,
) -> int:
    """Получить историю продаж для предметов.

    Returns:
        Количество предметов с сохранённой историей.
    """
    logger.info("═══ Фаза 4: Сбор истории продаж (pricehistory) ═══")

    # Предметы у которых ещё нет истории
    subq = select(SteamSalesHistory.hash_name).distinct()
    query = (
        select(SteamItem.hash_name)
        .where(SteamItem.hash_name.notin_(subq))
        .order_by(SteamItem.sell_listings.desc())
    )
    if limit > 0:
        query = query.limit(limit)

    result = await session.execute(query)
    items = result.scalars().all()
    total = len(items)
    logger.info(f"Предметов для получения истории: {total}")

    updated = 0

    for i, hash_name in enumerate(items, 1):
        history = await steam.get_price_history(hash_name, app_id=APP_ID)
        if not history:
            logger.debug(f"[{i}/{total}] Нет истории: {hash_name}")
            continue

        rows_inserted = 0
        for entry in history:
            if len(entry) < 3:
                continue
            date_str, price, volume_str = entry[0], entry[1], entry[2]

            # Парсим дату Steam: "Mar 07 2026 12: +0"
            try:
                clean = date_str.split(": +")[0].strip()
                dt = datetime.strptime(clean, "%b %d %Y %H")
                sale_date = dt.strftime("%Y-%m-%d %H:00:00")
            except (ValueError, IndexError):
                continue

            vol = int(volume_str.replace(",", "")) if isinstance(volume_str, str) else int(volume_str)

            # INSERT ... ON CONFLICT DO NOTHING
            stmt = pg_insert(SteamSalesHistory).values(
                hash_name=hash_name,
                sale_date=sale_date,
                price_usd=float(price),
                volume=vol,
            ).on_conflict_do_nothing(
                constraint="uq_steam_sales_hash_date",
            )
            await session.execute(stmt)
            rows_inserted += 1

        if rows_inserted > 0:
            updated += 1

        if i % 10 == 0:
            await session.commit()
            logger.info(f"Прогресс истории: {i}/{total} ({i*100//total}%), обновлено: {updated}")

    await session.commit()
    logger.info(f"Фаза 4 завершена: {updated}/{total} предметов с историей")
    return updated


# ─────────────────────────────────────────────────────
# Отчёт
# ─────────────────────────────────────────────────────

async def print_report(session: AsyncSession) -> None:
    """Вывести сводку по БД."""
    total = (await session.execute(
        select(func.count()).select_from(SteamItem)
    )).scalar() or 0

    with_prices = (await session.execute(
        select(func.count()).select_from(SteamItem).where(SteamItem.median_price_usd.isnot(None))
    )).scalar() or 0

    with_orders = (await session.execute(
        select(func.count()).select_from(SteamItem).where(SteamItem.buy_order_price_usd.isnot(None))
    )).scalar() or 0

    with_history = (await session.execute(
        select(func.count(func.distinct(SteamSalesHistory.hash_name)))
    )).scalar() or 0

    history_rows = (await session.execute(
        select(func.count()).select_from(SteamSalesHistory)
    )).scalar() or 0

    row = (await session.execute(
        select(
            func.min(SteamItem.sell_price_usd),
            func.max(SteamItem.sell_price_usd),
            func.avg(SteamItem.sell_price_usd),
        ).where(SteamItem.sell_price_usd > 0)
    )).one_or_none()
    price_min, price_max, price_avg = row if row else (0, 0, 0)

    # Топ-10 самых дорогих
    top_expensive = (await session.execute(
        select(SteamItem.hash_name, SteamItem.sell_price_usd, SteamItem.sell_listings)
        .order_by(SteamItem.sell_price_usd.desc())
        .limit(10)
    )).all()

    # Топ-10 самых торгуемых
    top_volume = (await session.execute(
        select(SteamItem.hash_name, SteamItem.volume_24h, SteamItem.median_price_usd)
        .where(SteamItem.volume_24h.isnot(None))
        .order_by(SteamItem.volume_24h.desc())
        .limit(10)
    )).all()

    logger.info("═══════════════════════════════════════════════════")
    logger.info(f"  Всего предметов:          {total}")
    logger.info(f"  С ценами (priceoverview): {with_prices}")
    logger.info(f"  С ордерами:               {with_orders}")
    logger.info(f"  С историей продаж:        {with_history}")
    logger.info(f"  Записей истории:          {history_rows}")
    if price_min is not None:
        logger.info(f"  Цена мин/макс/сред:       ${price_min:.2f} / ${price_max:.2f} / ${float(price_avg):.2f}")
    logger.info("═══════════════════════════════════════════════════")

    if top_expensive:
        logger.info("  Топ-10 дорогих:")
        for name, price, listings in top_expensive:
            logger.info(f"    ${price:>10.2f}  ({listings} лотов)  {name}")

    if top_volume:
        logger.info("  Топ-10 по объёму (24ч):")
        for name, vol, med in top_volume:
            med_str = f"${med:.2f}" if med else "N/A"
            logger.info(f"    {vol:>6} продаж  (медиана {med_str})  {name}")


# ─────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    setup_logging("DEBUG" if args.verbose else "INFO")
    logger.info("=== Сканер Steam Market — Dota 2 ===")

    await init_db()

    steam = SteamClient()

    try:
        async with async_session() as session:
            # Фаза 1 — всегда
            await phase1_scan_all_items(steam, session, limit=args.limit, start_offset=args.start)

            # Фаза 2 — обогащение ценами
            if args.enrich:
                await phase2_enrich_prices(steam, session, limit=args.limit)

            # Фаза 3 — ордера на покупку
            if args.orders:
                await phase3_fetch_orders(steam, session, limit=args.limit)

            # Фаза 4 — история продаж
            if args.history:
                await phase4_fetch_history(steam, session, limit=args.limit)

            # Отчёт
            await print_report(session)

    finally:
        await steam.close()

    logger.info("=== Сканирование завершено ===")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сканер предметов Dota 2 на Steam Market")
    parser.add_argument("--enrich", action="store_true", help="Обогатить ценами (priceoverview, медленно ~3с/предмет)")
    parser.add_argument("--orders", action="store_true", help="Получить ордера на покупку (ещё медленнее, 2 запроса/предмет)")
    parser.add_argument("--history", action="store_true", help="Получить историю продаж (требует steamLoginSecure)")
    parser.add_argument("--start", type=int, default=0, help="Начать с позиции N (для продолжения прерванного скана)")
    parser.add_argument("--limit", type=int, default=0, help="Ограничить количество предметов (0 = все)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Подробные логи (DEBUG)")
    parser.add_argument("--all", "-a", action="store_true", help="Все фазы (enrich + orders + history)")
    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.all:
        parsed.enrich = True
        parsed.orders = True
        parsed.history = True
    asyncio.run(main(parsed))
