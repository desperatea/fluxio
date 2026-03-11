"""Арбитраж CS2DT-first: сначала сканируем CS2DT, потом проверяем/обогащаем через Steam.

Поток данных:
1. CS2DT search_market (все страницы, Dota 2) → собираем все предметы
2. Фильтр по цене CS2DT (<= MAX_CS2DT_PRICE)
3. Проверяем каждый предмет в нашей PostgreSQL БД (steam_items)
   3.1 Если нет в БД → запрашиваем Steam priceoverview → добавляем в БД
   3.2 Если есть, но данные устарели → обновляем Steam
4. Потенциально прибыльные → перепроверяем цену Steam (pricehistory)
5. Записываем актуальную цену Steam + время последней проверки в БД
6. Стандартный алгоритм: верификация → покупка (dry-run)

Запуск:
    python scripts/cs2dt_first_buy.py
    python scripts/cs2dt_first_buy.py --dry-run          # только симуляция (по умолчанию)
    python scripts/cs2dt_first_buy.py --live              # реальные покупки
    python scripts/cs2dt_first_buy.py --skip-steam        # не обращаться к Steam, только кэш
"""

import asyncio
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from loguru import logger
from sqlalchemy import select, func, delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_helper import async_session, init_db
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.api.steam_client import SteamClient
from fluxio.db.models import SteamItem, SteamSalesHistory, ArbitragePurchase
from fluxio.utils.logger import setup_logging

APP_ID = 570

# ──────────────────────────────────────────────────────
# Параметры арбитража
# ──────────────────────────────────────────────────────
MIN_STEAM_PRICE = 0.09          # Мин. цена Steam USD
MIN_CS2DT_PRICE = 0.03          # Мин. цена CS2DT (отсечь мусорные $0.01 листинги)
MIN_PROFIT_PCT = 45.0           # Мин. прибыль % после комиссии
STEAM_FEE_RATE = 0.15           # Комиссия Steam (5% Valve + 10% Dota 2)
MAX_CS2DT_PRICE = 0.80          # Макс. цена предмета на CS2DT ($)
ANOMALY_PRICE_RATIO = 50        # Steam/CS2DT > 50 = аномалия, нужна ручная проверка
MIN_MONTHLY_SALES = 60          # Мин. продаж за 30 дней
MAX_QTY_PER_ITEM = 15           # Макс. одинаковых предметов
MAX_SPEND_PER_ITEM = 5.00       # Макс. трата на один hash_name ($)
DAILY_TARGET = 50.0             # Целевой дневной объём покупок ($)
STALE_HOURS = 12                # Данные Steam считаются устаревшими через N часов
HISTORY_STALE_DAYS = 3          # Перезагрузить историю продаж если последняя продажа > N дней
TRADE_URL = os.getenv("TRADE_URL", "").strip("'\" ")


@dataclass
class CS2DTItem:
    """Предмет с рынка CS2DT."""
    item_id: int
    market_hash_name: str
    item_name: str
    price_usd: float
    quantity: int
    auto_deliver_price: float | None = None
    auto_deliver_quantity: int = 0


@dataclass
class Candidate:
    """Кандидат на покупку."""
    hash_name: str
    cs2dt_price: float
    cs2dt_quantity: int
    steam_price: float = 0.0
    steam_listings: int = 0
    profit: float = 0.0
    profit_pct: float = 0.0
    # Данные из БД / Steam
    median_price: float | None = None
    lowest_price: float | None = None
    volume_24h: int | None = None
    buy_order_price: float | None = None
    daily_sales: float | None = None
    total_sales_30d: int = 0
    data_source: str = ""
    history_confirms_price: bool = False
    verified: bool = False
    buy_quantity: int = 1
    in_db: bool = False


# ──────────────────────────────────────────────────────
# Шаг 1: Сканирование CS2DT
# ──────────────────────────────────────────────────────

async def step1_scan_cs2dt(cs2dt: CS2DTClient) -> list[CS2DTItem]:
    """Получить все предметы Dota 2 с CS2DT через search_market с пагинацией."""
    logger.info("Шаг 1: Сканирование рынка CS2DT (Dota 2)...")

    all_items: dict[str, CS2DTItem] = {}
    page = 1
    total_pages = 1

    while page <= total_pages:
        try:
            data = await cs2dt.search_market(
                app_id=APP_ID,
                page=page,
                limit=50,
                order_by=1,  # цена ↑
                max_price=str(MAX_CS2DT_PRICE),
            )
        except CS2DTAPIError as e:
            logger.error(f"  Ошибка CS2DT search page {page}: {e}")
            break

        if not data or not isinstance(data, dict):
            break

        items_list = data.get("list") or []
        total = int(data.get("total", 0))
        total_pages = int(data.get("pages", 1))

        if not items_list:
            break

        for item_data in items_list:
            hash_name = item_data.get("marketHashName", "")
            if not hash_name:
                continue

            price_info = item_data.get("priceInfo") or {}
            price_str = price_info.get("price") or item_data.get("price")
            if not price_str:
                continue

            price = float(price_str)
            if price < MIN_CS2DT_PRICE or price > MAX_CS2DT_PRICE:
                continue

            item_id = int(item_data.get("itemId", 0))
            quantity = int(price_info.get("quantity", 0) or item_data.get("quantity", 0))

            auto_price_str = price_info.get("autoDeliverPrice")
            auto_price = float(auto_price_str) if auto_price_str else None
            auto_qty = int(price_info.get("autoDeliverQuantity") or 0)

            # Дедупликация по hash_name — берём минимальную цену
            if hash_name not in all_items or price < all_items[hash_name].price_usd:
                all_items[hash_name] = CS2DTItem(
                    item_id=item_id,
                    market_hash_name=hash_name,
                    item_name=item_data.get("itemName", hash_name),
                    price_usd=price,
                    quantity=quantity,
                    auto_deliver_price=auto_price,
                    auto_deliver_quantity=auto_qty,
                )

        if page % 10 == 0 or page == total_pages:
            logger.info(
                f"  CS2DT: страница {page}/{total_pages}, "
                f"собрано {len(all_items)} уникальных предметов"
            )

        page += 1

    result = list(all_items.values())
    logger.info(
        f"Шаг 1 завершён: {len(result)} уникальных предметов с CS2DT "
        f"(цена <= ${MAX_CS2DT_PRICE})"
    )
    return result


# ──────────────────────────────────────────────────────
# Шаг 2: Проверка в БД + предварительный расчёт профита
# ──────────────────────────────────────────────────────

async def step2_check_db(
    session: AsyncSession,
    cs2dt_items: list[CS2DTItem],
) -> tuple[list[Candidate], list[Candidate]]:
    """Проверить предметы в БД, разделить на 'есть данные Steam' и 'нет данных'.

    Returns:
        (candidates_with_steam, candidates_without_steam)
    """
    logger.info(f"Шаг 2: Проверка {len(cs2dt_items)} предметов в БД...")

    with_steam: list[Candidate] = []
    without_steam: list[Candidate] = []

    for item in cs2dt_items:
        result = await session.execute(
            select(
                SteamItem.sell_price_usd, SteamItem.sell_listings, SteamItem.volume_24h,
                SteamItem.median_price_usd, SteamItem.buy_order_price_usd, SteamItem.updated_at,
            )
            .where(SteamItem.hash_name == item.market_hash_name)
        )
        row = result.one_or_none()

        candidate = Candidate(
            hash_name=item.market_hash_name,
            cs2dt_price=item.price_usd,
            cs2dt_quantity=item.quantity,
        )

        if row and row[0] is not None and row[0] > 0:
            # Есть данные Steam в БД
            steam_price = row[0]
            steam_listings = row[1] or 0
            volume_24h = row[2]
            median_price = row[3]
            buy_order_price = row[4]
            updated_at = row[5]

            candidate.steam_price = steam_price
            candidate.steam_listings = steam_listings
            candidate.volume_24h = volume_24h
            candidate.median_price = median_price
            candidate.buy_order_price = buy_order_price
            candidate.in_db = True

            # Предварительный расчёт профита
            steam_after_fee = steam_price * (1 - STEAM_FEE_RATE)
            profit = steam_after_fee - item.price_usd
            profit_pct = (profit / item.price_usd) * 100 if item.price_usd > 0 else 0

            candidate.profit = round(profit, 4)
            candidate.profit_pct = round(profit_pct, 2)

            # Проверяем свежесть данных
            is_stale = True
            if updated_at:
                try:
                    ut = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - ut).total_seconds() / 3600
                    is_stale = age_hours > STALE_HOURS
                except (ValueError, TypeError):
                    is_stale = True

            if steam_price >= MIN_STEAM_PRICE:
                if profit_pct >= MIN_PROFIT_PCT:
                    with_steam.append(candidate)
                elif profit_pct >= MIN_PROFIT_PCT * 0.7:
                    # Близко к порогу — тоже стоит проверить свежие данные
                    with_steam.append(candidate)
        else:
            # Нет данных Steam — нужно загрузить
            without_steam.append(candidate)

    logger.info(
        f"Шаг 2 завершён: "
        f"{len(with_steam)} с данными Steam (потенциально прибыльных), "
        f"{len(without_steam)} без данных Steam"
    )

    # Топ потенциально прибыльных
    profitable = [c for c in with_steam if c.profit_pct >= MIN_PROFIT_PCT]
    if profitable:
        profitable.sort(key=lambda c: c.profit_pct, reverse=True)
        logger.info(f"  Предварительно прибыльных (>= {MIN_PROFIT_PCT}%): {len(profitable)}")
        for c in profitable[:10]:
            logger.info(
                f"    {c.profit_pct:>6.1f}%  CS2DT ${c.cs2dt_price:.2f} → "
                f"Steam ${c.steam_price:.2f}  [{c.cs2dt_quantity}]  {c.hash_name[:50]}"
            )

    return with_steam, without_steam


# ──────────────────────────────────────────────────────
# Шаг 3: Обогащение через Steam API
# ──────────────────────────────────────────────────────

async def step3_enrich_steam(
    steam: SteamClient,
    session: AsyncSession,
    candidates_no_steam: list[Candidate],
    candidates_with_steam: list[Candidate],
) -> list[Candidate]:
    """Загрузить данные Steam для предметов без данных, перепроверить потенциальных.

    Returns:
        Обновлённый список всех кандидатов с актуальными данными Steam.
    """
    # Приоритет обогащения:
    # 1. Предметы без данных Steam вообще (новые)
    # 2. Потенциально прибыльные с устаревшими данными
    to_enrich: list[Candidate] = []

    # Все новые предметы (ротация прокси снимает лимит Steam)
    new_sorted = sorted(candidates_no_steam, key=lambda c: c.cs2dt_price)
    to_enrich.extend(new_sorted)
    logger.info(f"  Новых предметов для обогащения: {len(new_sorted)}")

    # Прибыльные — перепроверяем все
    profitable_existing = [
        c for c in candidates_with_steam if c.profit_pct >= MIN_PROFIT_PCT * 0.7
    ]
    to_enrich.extend(profitable_existing)

    if not to_enrich:
        logger.info("Шаг 3: Нечего обогащать через Steam")
        return candidates_with_steam

    logger.info(
        f"Шаг 3: Обогащение Steam для {len(to_enrich)} предметов "
        f"({len(new_sorted)} новых + {len(profitable_existing)} проверка прибыльных)..."
    )

    now = datetime.now(timezone.utc).isoformat()
    enriched_count = 0
    num_channels = len(steam._proxy_urls)  # кол-во параллельных каналов

    async def _enrich_one(c: Candidate) -> bool:
        """Обогатить один предмет через Steam priceoverview."""
        overview = await steam.get_price_overview(c.hash_name, app_id=APP_ID)
        if not overview:
            return False

        median = steam._parse_price_string(overview.get("median_price", ""))
        lowest = steam._parse_price_string(overview.get("lowest_price", ""))
        volume_str = overview.get("volume", "0")
        volume = int(volume_str.replace(",", "")) if volume_str else 0

        c.median_price = median
        c.lowest_price = lowest
        c.volume_24h = volume

        if median and median > 0:
            c.steam_price = median
        elif lowest and lowest > 0:
            c.steam_price = lowest

        if c.steam_price > 0:
            steam_after_fee = c.steam_price * (1 - STEAM_FEE_RATE)
            c.profit = round(steam_after_fee - c.cs2dt_price, 4)
            c.profit_pct = round((c.profit / c.cs2dt_price) * 100, 2) if c.cs2dt_price > 0 else 0

        c.in_db = True
        return True

    # Параллельное обогащение пакетами по числу каналов
    batch_size = num_channels
    total = len(to_enrich)

    for batch_start in range(0, total, batch_size):
        batch = to_enrich[batch_start:batch_start + batch_size]
        results = await asyncio.gather(
            *[_enrich_one(c) for c in batch],
            return_exceptions=True,
        )

        # Сохраняем результаты в БД
        for c, ok in zip(batch, results):
            if ok is True:
                existing = (await session.execute(
                    select(SteamItem).where(SteamItem.hash_name == c.hash_name)
                )).scalar_one_or_none()

                if existing:
                    if c.steam_price:
                        existing.sell_price_usd = c.steam_price
                    existing.median_price_usd = c.median_price
                    existing.lowest_price_usd = c.lowest_price
                    existing.volume_24h = c.volume_24h
                    existing.updated_at = now
                else:
                    new_item = SteamItem(
                        hash_name=c.hash_name,
                        name=c.hash_name,
                        sell_price_usd=c.steam_price or 0,
                        sell_listings=0,
                        median_price_usd=c.median_price,
                        lowest_price_usd=c.lowest_price,
                        volume_24h=c.volume_24h,
                        first_seen_at=now,
                        updated_at=now,
                    )
                    session.add(new_item)
                enriched_count += 1

        processed = min(batch_start + batch_size, total)
        if processed % 30 == 0 or processed == total:
            await session.commit()
            logger.info(f"  Steam: {processed}/{total} ({enriched_count} обогащено)")

    await session.commit()
    logger.info(f"Шаг 3 завершён: обогащено {enriched_count} предметов через Steam")

    # --- Подгрузка buy orders для предметов без данных ---
    all_enriched = list(to_enrich) + [
        c for c in candidates_with_steam if c.hash_name not in {x.hash_name for x in to_enrich}
    ]
    need_bo = [
        c for c in all_enriched
        if c.buy_order_price is None and c.steam_price >= MIN_STEAM_PRICE and c.profit_pct >= MIN_PROFIT_PCT
    ]
    if need_bo:
        logger.info(f"Шаг 3.1: Загрузка buy orders для {len(need_bo)} предметов...")
        bo_loaded = 0

        async def _load_buy_order(c: Candidate) -> bool:
            """Загрузить buy order price для одного предмета."""
            # Получить item_nameid из БД или API
            item_obj = (await session.execute(
                select(SteamItem).where(SteamItem.hash_name == c.hash_name)
            )).scalar_one_or_none()
            nameid = item_obj.item_nameid if item_obj else None

            if not nameid:
                nameid = await steam.get_item_nameid(c.hash_name, app_id=APP_ID)
                if nameid and item_obj:
                    item_obj.item_nameid = nameid
                elif not nameid:
                    return False

            histogram = await steam.get_item_orders_histogram(nameid)
            if not histogram:
                return False

            buy_price_cents = histogram.get("highest_buy_order")
            if buy_price_cents:
                c.buy_order_price = int(buy_price_cents) / 100.0
                if item_obj:
                    item_obj.buy_order_price_usd = c.buy_order_price
                return True
            return False

        for batch_start in range(0, len(need_bo), batch_size):
            batch = need_bo[batch_start:batch_start + batch_size]
            results = await asyncio.gather(
                *[_load_buy_order(c) for c in batch],
                return_exceptions=True,
            )
            bo_loaded += sum(1 for r in results if r is True)
            if (batch_start + batch_size) % 50 == 0 or batch_start + batch_size >= len(need_bo):
                await session.commit()

        await session.commit()
        logger.info(f"Шаг 3.1 завершён: загружено {bo_loaded} buy orders")

    # Собираем все кандидаты с данными
    all_candidates = []
    seen: set[str] = set()

    # Сначала обогащённые (свежие данные)
    for c in to_enrich:
        if c.hash_name not in seen and c.steam_price >= MIN_STEAM_PRICE:
            seen.add(c.hash_name)
            all_candidates.append(c)

    # Потом остальные из кэша
    for c in candidates_with_steam:
        if c.hash_name not in seen and c.steam_price >= MIN_STEAM_PRICE:
            seen.add(c.hash_name)
            all_candidates.append(c)

    # Фильтр по минимальной прибыли
    profitable = [c for c in all_candidates if c.profit_pct >= MIN_PROFIT_PCT]
    profitable.sort(key=lambda c: c.profit_pct, reverse=True)

    logger.info(f"  Всего с данными Steam: {len(all_candidates)}")
    logger.info(f"  Прибыльных (>= {MIN_PROFIT_PCT}%): {len(profitable)}")

    return profitable


# ──────────────────────────────────────────────────────
# Шаг 4: Глубокая верификация через историю Steam
# ──────────────────────────────────────────────────────

async def step4_verify(
    steam: SteamClient,
    session: AsyncSession,
    candidates: list[Candidate],
    already_bought_counts: dict[str, int],
    skip_steam: bool = False,
) -> tuple[list[Candidate], list[Candidate]]:
    """Верифицировать кандидатов: история продаж, объёмы, лимиты.

    Returns:
        (verified, anomalies) — прошедшие верификацию и аномальные для ручной проверки.
    """
    logger.info(f"Шаг 4: Верификация {len(candidates)} кандидатов...")

    verified: list[Candidate] = []
    anomalies: list[Candidate] = []
    reject_reasons: dict[str, int] = {}
    now = datetime.now(timezone.utc).isoformat()

    for idx, c in enumerate(candidates, 1):
        if idx <= 3 or idx % 50 == 0:
            logger.info(f"  Верификация [{idx}/{len(candidates)}]: {c.hash_name[:50]}")

        # --- Проверка аномальной цены (скам-детектор) ---
        if c.steam_price > 0 and c.cs2dt_price > 0:
            price_ratio = c.steam_price / c.cs2dt_price
            if price_ratio > ANOMALY_PRICE_RATIO:
                anomalies.append(c)
                reject_reasons["аномальная цена (ratio > 50)"] = reject_reasons.get(
                    "аномальная цена (ratio > 50)", 0
                ) + 1
                continue

        # --- Проверяем историю продаж в БД ---
        result = await session.execute(
            select(
                func.coalesce(func.sum(SteamSalesHistory.volume), 0),
                func.count(func.distinct(func.substr(SteamSalesHistory.sale_date, 1, 10))),
            )
            .where(SteamSalesHistory.hash_name == c.hash_name)
            .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
        )
        hist_row = result.one_or_none()
        hist_volume = hist_row[0] if hist_row else 0
        hist_days = hist_row[1] if hist_row else 0
        has_history = hist_volume > 0

        # Проверяем свежесть истории: если последняя продажа > HISTORY_STALE_DAYS — перезагрузить
        needs_refresh = False
        if has_history:
            max_date_result = await session.execute(
                select(func.max(SteamSalesHistory.sale_date))
                .where(SteamSalesHistory.hash_name == c.hash_name)
            )
            max_date_row = max_date_result.one_or_none()
            if max_date_row and max_date_row[0]:
                try:
                    max_date = datetime.strptime(max_date_row[0], "%Y-%m-%d %H:00:00")
                    age_days = (datetime.now() - max_date).total_seconds() / 86400
                    if age_days > HISTORY_STALE_DAYS:
                        needs_refresh = True
                        logger.info(
                            f"    → История устарела для {c.hash_name[:50]} "
                            f"({age_days:.0f}д назад), перезагрузка..."
                        )
                except (ValueError, TypeError):
                    needs_refresh = True

        # Загружаем pricehistory если нет данных или устарели
        if (not has_history or needs_refresh) and not skip_steam:
            if needs_refresh:
                await session.execute(
                    delete(SteamSalesHistory).where(SteamSalesHistory.hash_name == c.hash_name)
                )
            logger.info(f"    → pricehistory для {c.hash_name[:50]}...")
            history = await steam.get_price_history(c.hash_name, app_id=APP_ID)
            if history:
                for entry in history:
                    if len(entry) < 3:
                        continue
                    date_str, price, volume_str = entry[0], entry[1], entry[2]
                    try:
                        clean = date_str.split(": +")[0].strip()
                        dt = datetime.strptime(clean, "%b %d %Y %H")
                        sale_date = dt.strftime("%Y-%m-%d %H:00:00")
                    except (ValueError, IndexError):
                        continue

                    vol = int(volume_str.replace(",", "")) if isinstance(volume_str, str) else int(volume_str)
                    stmt = pg_insert(SteamSalesHistory).values(
                        hash_name=c.hash_name,
                        sale_date=sale_date,
                        price_usd=float(price),
                        volume=vol,
                    ).on_conflict_do_nothing(
                        constraint="uq_steam_sales_hash_date",
                    )
                    await session.execute(stmt)

                # Перечитываем
                result = await session.execute(
                    select(
                        func.coalesce(func.sum(SteamSalesHistory.volume), 0),
                        func.count(func.distinct(func.substr(SteamSalesHistory.sale_date, 1, 10))),
                    )
                    .where(SteamSalesHistory.hash_name == c.hash_name)
                    .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
                )
                hist_row = result.one_or_none()
                hist_volume = hist_row[0] if hist_row else 0
                hist_days = hist_row[1] if hist_row else 0
                has_history = hist_volume > 0

                if idx % 5 == 0:
                    await session.commit()

        # --- Определяем объём продаж ---
        # Без реальной истории продаж — не пропускаем. Не додумываем данные.
        if has_history:
            c.total_sales_30d = hist_volume
            c.daily_sales = hist_volume / max(hist_days, 1) if hist_days > 0 else 0.0
            c.data_source = "история"
        else:
            reject_reasons["нет истории продаж"] = reject_reasons.get("нет истории продаж", 0) + 1
            continue

        if c.total_sales_30d < MIN_MONTHLY_SALES:
            reject_reasons["мало продаж"] = reject_reasons.get("мало продаж", 0) + 1
            continue

        # --- Проверка цены по истории ---
        if has_history:
            price_result = await session.execute(
                select(SteamSalesHistory.price_usd)
                .where(SteamSalesHistory.hash_name == c.hash_name)
                .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
                .order_by(SteamSalesHistory.price_usd)
            )
            history_prices = [r[0] for r in price_result.all()]
            if history_prices:
                hist_median = history_prices[len(history_prices) // 2]
                # Допуск: текущая цена не выше 1.5x медианы и не ниже 0.75x медианы
                if c.steam_price > hist_median * 1.5:
                    reject_reasons["цена >> истории"] = reject_reasons.get("цена >> истории", 0) + 1
                    continue
                if c.steam_price < hist_median * 0.75:
                    reject_reasons["цена << истории (>15%)"] = reject_reasons.get("цена << истории (>15%)", 0) + 1
                    continue
                c.history_confirms_price = True

        # --- ОБЯЗАТЕЛЬНО: без подтверждённой истории не покупаем ---
        if not c.history_confirms_price:
            reject_reasons["нет подтверждения истории"] = reject_reasons.get("нет подтверждения истории", 0) + 1
            continue

        # --- Лимит покупок ---
        already_have = already_bought_counts.get(c.hash_name, 0)
        remaining_allowed = max(0, MAX_QTY_PER_ITEM - already_have)
        if remaining_allowed == 0:
            reject_reasons["лимит шт достигнут"] = reject_reasons.get("лимит шт достигнут", 0) + 1
            continue

        # --- Количество для покупки ---
        # Базовый qty по daily_sales (тиры по ликвидности)
        if c.daily_sales and c.daily_sales > 0:
            if c.daily_sales >= 50:
                base_qty = max(1, int(c.daily_sales * 1.0))   # 1 день для популярных
            elif c.daily_sales >= 10:
                base_qty = max(1, int(c.daily_sales * 0.5))   # полдня
            else:
                base_qty = max(1, min(3, int(c.daily_sales)))  # макс 3 для редких
        else:
            base_qty = 1

        # Бонус от buy order (альтернативный сигнал ликвидности)
        bo_bonus = 0
        if c.buy_order_price and c.buy_order_price > 0:
            bo_net = math.floor(c.buy_order_price * (1 - STEAM_FEE_RATE) * 100) / 100
            bo_profit_pct = (bo_net - c.cs2dt_price) / c.cs2dt_price * 100 if c.cs2dt_price > 0 else 0
            if bo_profit_pct >= 40:
                bo_bonus = 3

        c.buy_quantity = min(base_qty + bo_bonus, remaining_allowed, c.cs2dt_quantity)
        max_by_budget = max(1, int(MAX_SPEND_PER_ITEM / c.cs2dt_price))
        c.buy_quantity = min(c.buy_quantity, max_by_budget)

        c.verified = True
        verified.append(c)

    await session.commit()

    # Статистика отказов
    if reject_reasons:
        logger.info("  Причины отклонения:")
        for reason, count in sorted(reject_reasons.items(), key=lambda x: -x[1]):
            logger.info(f"    {reason}: {count}")

    # Сортировка: score = profit_pct * daily_sales
    verified.sort(key=lambda c: c.profit_pct * (c.daily_sales or 0), reverse=True)

    total_spend = sum(c.buy_quantity * c.cs2dt_price for c in verified)
    total_items = sum(c.buy_quantity for c in verified)
    total_profit = sum(c.buy_quantity * c.profit for c in verified)

    logger.info(f"Шаг 4 завершён: верифицировано {len(verified)} из {len(candidates)}")
    logger.info(f"  Потенциальный объём: ${total_spend:.2f}, прибыль: +${total_profit:.2f}")

    for c in verified:
        logger.info(
            f"  ✓ {c.profit_pct:>6.1f}%  x{c.buy_quantity:<2}  "
            f"CS2DT ${c.cs2dt_price:.2f} → Steam ${c.steam_price:.2f}  "
            f"продаж/д={c.daily_sales:.1f}  [{c.data_source}]  "
            f"{c.hash_name[:50]}"
        )

    if anomalies:
        logger.warning(
            f"  ⚠ Аномальные предметы (Steam/CS2DT > {ANOMALY_PRICE_RATIO}x), "
            f"требуют ручной проверки: {len(anomalies)}"
        )
        for c in anomalies[:10]:
            ratio = c.steam_price / c.cs2dt_price if c.cs2dt_price > 0 else 0
            logger.warning(
                f"    ⚠ {ratio:.0f}x  CS2DT ${c.cs2dt_price:.2f} → "
                f"Steam ${c.steam_price:.2f}  {c.hash_name[:60]}"
            )

    return verified, anomalies


# ──────────────────────────────────────────────────────
# Шаг 4.5: Перепроверка свежести цен Steam перед покупкой
# ──────────────────────────────────────────────────────

PRICE_MAX_AGE_MINUTES = 30  # Максимальный возраст данных Steam перед покупкой

async def step4_5_refresh_prices(
    steam: SteamClient,
    session: AsyncSession,
    verified: list[Candidate],
) -> list[Candidate]:
    """Перепроверить цены Steam для кандидатов с устаревшими данными (>30 мин).

    Обязательный шаг перед покупкой: нельзя покупать по устаревшим ценам.
    """
    logger.info(f"Шаг 4.5: Проверка свежести цен для {len(verified)} кандидатов...")

    now = datetime.now(timezone.utc)
    stale: list[Candidate] = []
    fresh: list[Candidate] = []

    for c in verified:
        result = await session.execute(
            select(SteamItem.updated_at).where(SteamItem.hash_name == c.hash_name)
        )
        row = result.one_or_none()

        is_stale = True
        if row and row[0]:
            try:
                ut = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                age_minutes = (now - ut).total_seconds() / 60
                is_stale = age_minutes > PRICE_MAX_AGE_MINUTES
                if not is_stale:
                    fresh.append(c)
                    continue
            except (ValueError, TypeError):
                pass

        stale.append(c)

    logger.info(
        f"  Свежих (< {PRICE_MAX_AGE_MINUTES} мин): {len(fresh)}, "
        f"устаревших (нужно обновить): {len(stale)}"
    )

    if not stale:
        logger.info("Шаг 4.5: Все цены свежие, обновление не требуется")
        return verified

    # Параллельное обновление через прокси (как шаг 3)
    num_channels = len(steam._proxy_urls)
    batch_size = num_channels
    now_iso = now.isoformat()
    refreshed = 0
    dropped = 0

    async def _refresh_one(c: Candidate) -> bool:
        """Обновить цену одного предмета через Steam priceoverview."""
        overview = await steam.get_price_overview(c.hash_name, app_id=APP_ID)
        if not overview:
            return False

        median = steam._parse_price_string(overview.get("median_price", ""))
        lowest = steam._parse_price_string(overview.get("lowest_price", ""))
        volume_str = overview.get("volume", "0")
        volume = int(volume_str.replace(",", "")) if volume_str else 0

        new_price = median if median and median > 0 else (lowest if lowest and lowest > 0 else 0)
        if new_price <= 0:
            return False

        # Обновляем кандидата
        old_price = c.steam_price
        c.steam_price = new_price
        c.median_price = median
        c.lowest_price = lowest
        c.volume_24h = volume

        # Пересчитываем профит
        steam_after_fee = new_price * (1 - STEAM_FEE_RATE)
        c.profit = round(steam_after_fee - c.cs2dt_price, 4)
        c.profit_pct = round((c.profit / c.cs2dt_price) * 100, 2) if c.cs2dt_price > 0 else 0

        if abs(old_price - new_price) > 0.001:
            logger.info(
                f"  Цена обновлена: {c.hash_name[:45]} "
                f"${old_price:.2f} → ${new_price:.2f} ({c.profit_pct:.1f}%)"
            )

        return True

    for batch_start in range(0, len(stale), batch_size):
        batch = stale[batch_start:batch_start + batch_size]
        results = await asyncio.gather(
            *[_refresh_one(c) for c in batch],
            return_exceptions=True,
        )

        # Сохраняем обновлённые цены в БД
        for c, ok in zip(batch, results):
            if ok is True:
                item_obj = (await session.execute(
                    select(SteamItem).where(SteamItem.hash_name == c.hash_name)
                )).scalar_one_or_none()
                if item_obj:
                    item_obj.sell_price_usd = c.steam_price
                    item_obj.median_price_usd = c.median_price
                    item_obj.lowest_price_usd = c.lowest_price
                    item_obj.volume_24h = c.volume_24h
                    item_obj.updated_at = now_iso
                refreshed += 1
            elif isinstance(ok, Exception):
                logger.warning(f"  ⚠ Ошибка обновления цены {c.hash_name[:50]}: {ok}")
            else:
                logger.debug(f"  ⚠ Не удалось обновить цену: {c.hash_name[:50]}")

        processed = min(batch_start + batch_size, len(stale))
        if processed % 50 == 0 or processed == len(stale):
            await session.commit()
            logger.info(f"  Обновление цен: {processed}/{len(stale)}")

    await session.commit()

    # Пересобираем список: только те, кто всё ещё прибылен
    result_list: list[Candidate] = []
    for c in fresh + stale:
        if c.profit_pct >= MIN_PROFIT_PCT:
            result_list.append(c)
        else:
            dropped += 1
            logger.info(
                f"  ✗ Выбыл: {c.hash_name[:50]} "
                f"(профит {c.profit_pct:.1f}% < {MIN_PROFIT_PCT}%)"
            )

    result_list.sort(key=lambda c: c.profit_pct * (c.daily_sales or 0), reverse=True)

    logger.info(
        f"Шаг 4.5 завершён: обновлено {refreshed}, "
        f"выбыло {dropped} (профит < {MIN_PROFIT_PCT}%), "
        f"осталось {len(result_list)} кандидатов"
    )

    for c in result_list[:15]:
        logger.info(
            f"  ✓ {c.profit_pct:>6.1f}%  x{c.buy_quantity:<2}  "
            f"CS2DT ${c.cs2dt_price:.2f} → Steam ${c.steam_price:.2f}  "
            f"{c.hash_name[:50]}"
        )

    return result_list


# ──────────────────────────────────────────────────────
# Шаг 5: Покупка (dry-run или реальная)
# ──────────────────────────────────────────────────────

async def step5_buy(
    cs2dt: CS2DTClient,
    verified: list[Candidate],
    dry_run: bool = True,
) -> list[dict]:
    """Купить верифицированные предметы (или симулировать)."""
    if not verified:
        logger.info("Шаг 5: Нет верифицированных предметов для покупки")
        return []

    # Баланс
    balance_data = await cs2dt.get_balance()
    balance = float(balance_data.get("data", "0"))
    mode = "DRY-RUN" if dry_run else "БОЕВОЙ"
    logger.info(f"Шаг 5 [{mode}]: Баланс CS2DT: ${balance:.2f}")

    if balance <= 0 and not dry_run:
        logger.warning("Баланс CS2DT = $0, покупки невозможны")
        return []

    purchases: list[dict] = []
    remaining = balance
    daily_spent = 0.0

    for c in verified:
        if daily_spent >= DAILY_TARGET:
            logger.info(f"  Достигнут дневной лимит ${DAILY_TARGET:.0f}")
            break

        item_cost = c.buy_quantity * c.cs2dt_price
        if remaining < c.cs2dt_price and not dry_run:
            logger.info(f"  Недостаточно баланса (${remaining:.2f}) для {c.hash_name}")
            continue

        if dry_run:
            # Симуляция
            profit_total = c.buy_quantity * c.profit
            remaining -= item_cost
            daily_spent += item_cost

            purchase_info = {
                "hash_name": c.hash_name,
                "buy_price": c.cs2dt_price,
                "quantity": c.buy_quantity,
                "steam_price": c.steam_price,
                "profit_pct": c.profit_pct,
                "profit_total": round(profit_total, 4),
                "daily_sales": c.daily_sales,
                "data_source": c.data_source,
                "dry_run": True,
            }
            purchases.append(purchase_info)
            logger.info(
                f"  [DRY-RUN] КУПИЛИ БЫ: {c.hash_name} x{c.buy_quantity} "
                f"за ${item_cost:.2f} (прибыль +${profit_total:.2f}, {c.profit_pct:.1f}%)"
            )
        else:
            # Реальная покупка — перепроверяем цену
            try:
                fresh_prices = await cs2dt.get_prices_batch([c.hash_name], app_id=APP_ID)
                if not fresh_prices or not isinstance(fresh_prices, list):
                    logger.warning(f"  Не удалось перепроверить цену: {c.hash_name}")
                    continue

                fresh = fresh_prices[0]
                fresh_price = float(fresh.get("price", "0"))
                fresh_qty = int(fresh.get("quantity", 0))

                if fresh_price <= 0 or fresh_qty <= 0:
                    logger.warning(f"  Нет в наличии на CS2DT: {c.hash_name}")
                    continue

                if fresh_price > c.cs2dt_price * 1.05:
                    logger.warning(
                        f"  Цена выросла ${c.cs2dt_price:.2f} → ${fresh_price:.2f}: "
                        f"{c.hash_name}"
                    )
                    continue

                actual_price = fresh_price
            except CS2DTAPIError as e:
                logger.error(f"  Ошибка перепроверки цены: {e}")
                continue

            # Макс. цена для quick_buy: при которой прибыль >= MIN_PROFIT_PCT
            # steam_net = steam_price * (1 - fee)
            # profit_pct = (steam_net - buy_price) / buy_price * 100
            # => buy_price_max = steam_net / (1 + MIN_PROFIT_PCT/100)
            steam_net = c.steam_price * (1 - STEAM_FEE_RATE)
            max_buy_price = round(steam_net / (1 + MIN_PROFIT_PCT / 100), 2)
            # Не платить больше макс. порога
            max_buy_price = min(max_buy_price, MAX_CS2DT_PRICE)

            qty = min(
                c.buy_quantity,
                fresh_qty,
                max(1, int(remaining / actual_price)),
                max(1, int((DAILY_TARGET - daily_spent) / actual_price)),
            )

            logger.info(
                f"  ПОКУПКА: {c.hash_name} x{qty} "
                f"за ${actual_price:.2f}/шт (макс ${max_buy_price:.2f}, "
                f"прибыль {c.profit_pct:.1f}%)"
            )

            bought_count = 0
            consecutive_errors = 0
            for unit in range(qty):
                out_trade_no = str(uuid.uuid4())
                result = None

                # Попытка покупки с retry (API кеш обновляется ~3с)
                for attempt in range(3):
                    try:
                        result = await cs2dt.quick_buy(
                            out_trade_no=out_trade_no,
                            trade_url=TRADE_URL,
                            max_price=max_buy_price,
                            market_hash_name=c.hash_name,
                            app_id=APP_ID,
                            low_price=True,
                        )
                        consecutive_errors = 0
                        break
                    except CS2DTAPIError as e:
                        err_code = getattr(e, "error_code", 0) or 0
                        # 1014452 = "предмет не в продаже" — retry после паузы
                        if err_code == 1014452 and attempt < 2:
                            await asyncio.sleep(3)
                            out_trade_no = str(uuid.uuid4())  # новый ID
                            continue
                        # Другие ошибки или исчерпаны retry
                        logger.error(
                            f"  ✗ Ошибка покупки {c.hash_name} "
                            f"(шт {unit+1}/{qty}): {e}"
                        )
                        consecutive_errors += 1
                        result = None
                        break

                if result is None:
                    # 2+ ошибки подряд — лоты реально закончились
                    if consecutive_errors >= 2:
                        break
                    continue

                buy_price = result.get("buyPrice", actual_price)
                order_id = result.get("orderId", "")
                delivery = result.get("delivery", 0)

                remaining -= float(buy_price)
                daily_spent += float(buy_price)
                bought_count += 1

                actual_profit = c.steam_price * (1 - STEAM_FEE_RATE) - float(buy_price)
                purchases.append({
                    "hash_name": c.hash_name,
                    "buy_price": float(buy_price),
                    "quantity": 1,
                    "order_id": order_id,
                    "out_trade_no": out_trade_no,
                    "delivery": delivery,
                    "steam_price": c.steam_price,
                    "profit_pct": c.profit_pct,
                    "profit_total": round(actual_profit, 4),
                    "daily_sales": c.daily_sales,
                    "dry_run": False,
                })

                # Пауза между покупками одного предмета (кеш API)
                if unit < qty - 1:
                    await asyncio.sleep(2)

                if remaining < actual_price:
                    break

            if bought_count > 0:
                # Сумма реально потраченного (из последних bought_count записей)
                real_spent = sum(p["buy_price"] for p in purchases[-bought_count:])
                logger.info(
                    f"  ✓ КУПЛЕНО: {c.hash_name} x{bought_count} "
                    f"за ${real_spent:.2f} (${real_spent/bought_count:.2f}/шт)"
                )

    # Итого
    total_spent = sum(p["buy_price"] * p.get("quantity", 1) for p in purchases)
    total_profit = sum(
        p.get("profit_total", p["buy_price"] * (p["profit_pct"] / 100))
        for p in purchases
    )
    unique = len({p["hash_name"] for p in purchases})

    logger.info(f"\n{'=' * 70}")
    logger.info(f"  ИТОГО [{mode}]:")
    logger.info(f"    Баланс:            ${balance:.2f}")
    logger.info(f"    Предметов:         {unique} уникальных, {sum(p.get('quantity', 1) for p in purchases)} штук")
    logger.info(f"    Потрачено:         ${total_spent:.2f}")
    logger.info(f"    Ожид. прибыль:     +${total_profit:.2f}")
    if total_spent > 0:
        logger.info(f"    ROI:               {total_profit / total_spent * 100:.1f}%")
    logger.info(f"    Остаток:           ${balance - total_spent:.2f}")
    logger.info(f"{'=' * 70}")

    return purchases


# ──────────────────────────────────────────────────────
# Сохранение покупок
# ──────────────────────────────────────────────────────

async def save_purchases(session: AsyncSession, purchases: list[dict]) -> None:
    """Сохранить покупки в PostgreSQL."""
    now = datetime.now(timezone.utc).isoformat()
    for p in purchases:
        if p.get("dry_run"):
            continue  # Не сохраняем dry-run покупки
        purchase = ArbitragePurchase(
            hash_name=p["hash_name"],
            buy_price_usd=p["buy_price"],
            steam_price_usd=p["steam_price"],
            profit_pct=p["profit_pct"],
            daily_sales=p.get("daily_sales"),
            order_id=p.get("order_id"),
            out_trade_no=p.get("out_trade_no"),
            delivery=p.get("delivery"),
            purchased_at=now,
        )
        session.add(purchase)

    await session.commit()
    real_purchases = [p for p in purchases if not p.get("dry_run")]
    if real_purchases:
        logger.info(f"Покупки сохранены: {len(real_purchases)} записей")


ANOMALIES_PATH = Path(__file__).resolve().parent.parent / "data" / "anomalies.json"


def _save_anomalies(anomalies: list[Candidate]) -> None:
    """Сохранить аномальные предметы в JSON для ручной проверки через веб-дашборд."""
    # Загрузить существующие решения (approved/rejected)
    existing: dict[str, dict] = {}
    if ANOMALIES_PATH.exists():
        try:
            with open(ANOMALIES_PATH, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    existing[item["hash_name"]] = item
        except (json.JSONDecodeError, KeyError):
            pass

    timestamp = datetime.now(timezone.utc).isoformat()
    items = []
    for c in anomalies:
        ratio = c.steam_price / c.cs2dt_price if c.cs2dt_price > 0 else 0
        prev = existing.get(c.hash_name, {})
        items.append({
            "hash_name": c.hash_name,
            "cs2dt_price": c.cs2dt_price,
            "steam_price": c.steam_price,
            "price_ratio": round(ratio, 1),
            "cs2dt_quantity": c.cs2dt_quantity,
            "profit_pct": round(c.profit_pct, 1),
            "volume_24h": c.volume_24h,
            "daily_sales": c.daily_sales,
            "median_price": c.median_price,
            "lowest_price": c.lowest_price,
            "updated_at": timestamp,
            # Сохраняем предыдущее решение оператора
            "status": prev.get("status", "pending"),  # pending / approved / rejected
            "decided_at": prev.get("decided_at"),
        })

    ANOMALIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ANOMALIES_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    logger.info(f"Аномалии сохранены: {len(items)} шт → {ANOMALIES_PATH}")


# ──────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────

async def main(dry_run: bool = True, skip_steam: bool = False) -> None:
    setup_logging("INFO")

    mode = "DRY-RUN" if dry_run else "БОЕВОЙ"
    logger.info("=" * 70)
    logger.info(f"  АРБИТРАЖ CS2DT-FIRST → Steam (Dota 2) [{mode}]")
    logger.info("=" * 70)
    logger.info(f"  Параметры:")
    logger.info(f"    Макс. цена CS2DT:   ${MAX_CS2DT_PRICE}")
    logger.info(f"    Мин. цена Steam:    ${MIN_STEAM_PRICE}")
    logger.info(f"    Мин. прибыль:       {MIN_PROFIT_PCT}%")
    logger.info(f"    Мин. продаж/мес:    {MIN_MONTHLY_SALES}")
    logger.info(f"    Макс. штук:         {MAX_QTY_PER_ITEM}")
    logger.info(f"    Комиссия Steam:     {STEAM_FEE_RATE*100:.0f}%")
    logger.info(f"    Устаревание Steam:  {STALE_HOURS}ч")
    logger.info(f"    Skip Steam:         {skip_steam}")
    logger.info("=" * 70)

    if not dry_run and (not TRADE_URL or "partner=" not in TRADE_URL):
        logger.error("TRADE_URL не задан в .env — покупки невозможны")
        return

    await init_db()

    cs2dt = CS2DTClient()
    steam = SteamClient()

    try:
        # Шаг 1: Сканирование CS2DT
        cs2dt_items = await step1_scan_cs2dt(cs2dt)
        if not cs2dt_items:
            logger.error("Нет предметов на CS2DT")
            return

        # Загрузить количество уже купленных (за 7 дней)
        already_bought_counts: dict[str, int] = {}
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(ArbitragePurchase.hash_name, func.count())
                    .where(ArbitragePurchase.purchased_at >= text("(NOW() - INTERVAL '7 days')::text"))
                    .group_by(ArbitragePurchase.hash_name)
                )
                already_bought_counts = {r[0]: r[1] for r in result.all()}
        except Exception:
            pass

        total_prev = sum(already_bought_counts.values())
        if total_prev > 0:
            logger.info(
                f"Куплено за 7 дней: {total_prev} шт "
                f"({len(already_bought_counts)} уникальных)"
            )

        async with async_session() as session:
            # Шаг 2: Проверка в БД
            with_steam, without_steam = await step2_check_db(session, cs2dt_items)

            # Шаг 3: Обогащение через Steam
            if skip_steam:
                logger.info("Шаг 3: --skip-steam — используем только кэш БД")
                # Берём только тех, что уже прибыльны в кэше
                candidates = [c for c in with_steam if c.profit_pct >= MIN_PROFIT_PCT]
                candidates.sort(key=lambda c: c.profit_pct, reverse=True)
                logger.info(f"  Прибыльных из кэша: {len(candidates)}")
            else:
                candidates = await step3_enrich_steam(
                    steam, session, without_steam, with_steam,
                )

            if not candidates:
                logger.info("Нет кандидатов с достаточной прибылью")
                return

            # Шаг 4: Верификация
            verified, anomalies = await step4_verify(
                steam, session, candidates, already_bought_counts,
                skip_steam=skip_steam,
            )

            # Сохранить аномалии в JSON для ручной проверки через веб-дашборд
            if anomalies:
                _save_anomalies(anomalies)

            if not verified:
                logger.info("Ни один кандидат не прошёл верификацию")
                return

            # Шаг 4.5: Перепроверка свежести цен перед покупкой
            verified = await step4_5_refresh_prices(steam, session, verified)
            if not verified:
                logger.info("После перепроверки цен не осталось прибыльных кандидатов")
                return

            # Шаг 5: Покупка
            purchases = await step5_buy(cs2dt, verified, dry_run=dry_run)

            # Сохранить реальные покупки
            if purchases:
                await save_purchases(session, purchases)

    finally:
        await steam.close()
        await cs2dt.close()

    logger.info("=" * 70)
    logger.info("  АРБИТРАЖ ЗАВЕРШЁН")
    logger.info("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Арбитраж CS2DT-first → Steam")
    parser.add_argument("--live", action="store_true",
                        help="Реальные покупки (по умолчанию dry-run)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Симуляция без покупок (по умолчанию)")
    parser.add_argument("--skip-steam", action="store_true",
                        help="Не обращаться к Steam, использовать только кэш БД")
    args = parser.parse_args()

    is_dry_run = not args.live
    asyncio.run(main(dry_run=is_dry_run, skip_steam=args.skip_steam))
