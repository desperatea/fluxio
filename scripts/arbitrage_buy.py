"""Автоматический арбитраж: CS2DT → Steam Market (Dota 2).

Полный цикл:
1. Фильтр из steam_items: >=15 лотов Steam, цена >= $0.10
2. Сравнение с CS2DT: прибыль >= 50% после комиссии Steam (15%)
3. Загрузка фаз 2-4 Steam для отобранных (priceoverview, orders, history)
4. Верификация: >=5 продаж/день, цена соответствует истории
5. Покупка на CS2DT (макс $0.50/предмет, в рамках баланса)

Запуск:
    python scripts/arbitrage_buy.py
"""

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from loguru import logger
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_helper import async_session, init_db
from fluxio.api.steam_client import SteamClient
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.db.models import SteamItem, SteamSalesHistory, ArbitragePurchase
from fluxio.utils.logger import setup_logging

APP_ID = 570

# ──────────────────────────────────────────────────────
# Параметры арбитража
# ──────────────────────────────────────────────────────
MIN_STEAM_LISTINGS = 10       # Мин. лотов на продажу в Steam (было 15)
MIN_STEAM_PRICE = 0.09        # Мин. цена Steam USD (ниже — комиссия съедает прибыль)
MIN_PROFIT_PCT = 45.0         # Мин. прибыль % после комиссии (было 50%)
STEAM_FEE_RATE = 0.15         # Комиссия Steam (5% + 10% Dota 2)
MAX_CS2DT_PRICE = 0.80        # Макс. цена одного предмета на CS2DT (было $0.50)
MIN_MONTHLY_SALES = 60        # Мин. продаж за 30 дней (было 100, ~2/день минимум)
MAX_QTY_PER_ITEM = 15         # Макс. одинаковых предметов за раз
MAX_SPEND_PER_ITEM = 5.00     # Макс. трата на один hash_name ($)
DAILY_TARGET = 50.0           # Целевой дневной объём покупок ($)
TRADE_URL = os.getenv("TRADE_URL", "").strip("'\" ")


@dataclass
class Candidate:
    """Кандидат на покупку."""
    hash_name: str
    steam_price: float
    steam_listings: int
    cs2dt_price: float
    cs2dt_quantity: int
    profit: float
    profit_pct: float
    # Данные после фаз 2-4
    median_price: float | None = None
    lowest_price: float | None = None
    volume_24h: int | None = None
    buy_order_price: float | None = None
    daily_sales: float | None = None
    history_confirms_price: bool = False
    verified: bool = False
    buy_quantity: int = 1             # Сколько штук покупать


# ──────────────────────────────────────────────────────
# Шаг 1: Фильтрация из БД (PostgreSQL)
# ──────────────────────────────────────────────────────

async def step1_filter_items(session: AsyncSession) -> list[tuple]:
    """Предметы из steam_items: >=15 лотов, цена >= $0.10."""
    result = await session.execute(
        select(SteamItem.hash_name, SteamItem.sell_price_usd, SteamItem.sell_listings)
        .where(SteamItem.sell_listings >= MIN_STEAM_LISTINGS)
        .where(SteamItem.sell_price_usd >= MIN_STEAM_PRICE)
        .order_by(SteamItem.sell_listings.desc())
    )
    items = result.all()
    logger.info(
        f"Шаг 1: Из БД отфильтровано {len(items)} предметов "
        f"(>=  {MIN_STEAM_LISTINGS} лотов, >= ${MIN_STEAM_PRICE})"
    )
    return items


# ──────────────────────────────────────────────────────
# Шаг 2: Сравнение с CS2DT
# ──────────────────────────────────────────────────────

async def step2_compare_cs2dt(
    cs2dt: CS2DTClient,
    steam_items: list[tuple],
    already_purchased: set[str] | None = None,
) -> list[Candidate]:
    """Найти предметы с прибылью >= 50% после комиссии, цена CS2DT <= $0.50."""
    logger.info("Шаг 2: Запрос цен CS2DT и поиск выгодных предметов...")
    already_purchased = already_purchased or set()

    hash_names = [item[0] for item in steam_items]
    cs2dt_prices: dict[str, dict] = {}

    # Пакетный запрос по 200
    for i in range(0, len(hash_names), 200):
        batch = hash_names[i:i + 200]
        try:
            result = await cs2dt.get_prices_batch(batch, app_id=APP_ID)
            if isinstance(result, list):
                for item_data in result:
                    name = item_data.get("marketHashName", "")
                    if name:
                        cs2dt_prices[name] = item_data
        except CS2DTAPIError as e:
            logger.error(f"Ошибка CS2DT batch {i}: {e}")
            continue

        if (i + 200) % 1000 == 0:
            logger.info(f"  CS2DT цены: {i + len(batch)}/{len(hash_names)}")

    logger.info(f"  CS2DT вернул цены для {len(cs2dt_prices)} предметов")

    # Сравнение
    candidates: list[Candidate] = []
    steam_map = {item[0]: item for item in steam_items}

    for hash_name, cs2dt_data in cs2dt_prices.items():
        if hash_name in already_purchased:
            continue
        steam_item = steam_map.get(hash_name)
        if not steam_item:
            continue

        _, steam_price, steam_listings = steam_item

        cs2dt_price_str = cs2dt_data.get("price")
        if not cs2dt_price_str:
            continue
        cs2dt_price = float(cs2dt_price_str)
        if cs2dt_price <= 0 or cs2dt_price > MAX_CS2DT_PRICE:
            continue

        cs2dt_qty = int(cs2dt_data.get("quantity", 0))
        if cs2dt_qty <= 0:
            continue

        # Прибыль после комиссии Steam
        steam_after_fee = steam_price * (1 - STEAM_FEE_RATE)
        profit = steam_after_fee - cs2dt_price
        profit_pct = (profit / cs2dt_price) * 100

        if profit_pct >= MIN_PROFIT_PCT:
            candidates.append(Candidate(
                hash_name=hash_name,
                steam_price=steam_price,
                steam_listings=steam_listings,
                cs2dt_price=cs2dt_price,
                cs2dt_quantity=cs2dt_qty,
                profit=round(profit, 4),
                profit_pct=round(profit_pct, 2),
            ))

    candidates.sort(key=lambda c: c.profit_pct, reverse=True)
    logger.info(
        f"Шаг 2: Найдено {len(candidates)} кандидатов "
        f"(прибыль >= {MIN_PROFIT_PCT}%, цена CS2DT <= ${MAX_CS2DT_PRICE})"
    )

    for c in candidates[:20]:
        logger.info(
            f"  {c.profit_pct:>8.1f}%  "
            f"CS2DT ${c.cs2dt_price:.2f} → Steam ${c.steam_price:.2f}  "
            f"[{c.cs2dt_quantity}/{c.steam_listings}]  {c.hash_name[:55]}"
        )
    if len(candidates) > 20:
        logger.info(f"  ... и ещё {len(candidates) - 20}")

    return candidates


# ──────────────────────────────────────────────────────
# Шаг 3: Загрузка фаз 2-4 Steam для кандидатов
# ──────────────────────────────────────────────────────

async def step3_enrich_steam(
    steam: SteamClient,
    session: AsyncSession,
    candidates: list[Candidate],
) -> None:
    """Загрузить priceoverview, orders, history для кандидатов.

    Пропускает предметы, у которых уже есть данные из предыдущих запусков.
    """
    total = len(candidates)

    # Проверяем какие кандидаты уже обогащены
    need_enrich: list[Candidate] = []
    for c in candidates:
        result = await session.execute(
            select(SteamItem.volume_24h, SteamItem.buy_order_price_usd)
            .where(SteamItem.hash_name == c.hash_name)
        )
        row = result.one_or_none()
        if row and row[0] is not None:
            c.volume_24h = row[0]
            c.buy_order_price = row[1]
            hist_count = (await session.execute(
                select(func.count()).select_from(SteamSalesHistory)
                .where(SteamSalesHistory.hash_name == c.hash_name)
            )).scalar() or 0
            if hist_count > 0:
                continue
        need_enrich.append(c)

    logger.info(
        f"Шаг 3: Из {total} кандидатов {total - len(need_enrich)} уже обогащены, "
        f"нужно загрузить: {len(need_enrich)}"
    )

    if not need_enrich:
        logger.info("Шаг 3: Все кандидаты уже имеют данные Steam, пропускаем")
        return

    enrich_total = len(need_enrich)
    now = datetime.now(timezone.utc).isoformat()

    for i, c in enumerate(need_enrich, 1):
        # Phase 2: priceoverview
        overview = await steam.get_price_overview(c.hash_name, app_id=APP_ID)
        if overview:
            median = steam._parse_price_string(overview.get("median_price", ""))
            lowest = steam._parse_price_string(overview.get("lowest_price", ""))
            volume_str = overview.get("volume", "0")
            volume = int(volume_str.replace(",", "")) if volume_str else 0

            c.median_price = median
            c.lowest_price = lowest
            c.volume_24h = volume

            item_obj = (await session.execute(
                select(SteamItem).where(SteamItem.hash_name == c.hash_name)
            )).scalar_one_or_none()
            if item_obj:
                item_obj.median_price_usd = median
                item_obj.lowest_price_usd = lowest
                item_obj.volume_24h = volume
                item_obj.updated_at = now

        # Phase 3: orders (nameid + histogram)
        nameid = await steam.get_item_nameid(c.hash_name, app_id=APP_ID)
        if nameid:
            item_obj = (await session.execute(
                select(SteamItem).where(SteamItem.hash_name == c.hash_name)
            )).scalar_one_or_none()
            if item_obj:
                item_obj.item_nameid = nameid

            histogram = await steam.get_item_orders_histogram(nameid)
            if histogram:
                buy_cents = histogram.get("highest_buy_order")
                c.buy_order_price = int(buy_cents) / 100.0 if buy_cents else None

                buy_graph = histogram.get("buy_order_graph") or []
                sell_graph = histogram.get("sell_order_graph") or []
                buy_count = sum(int(e[1]) for e in buy_graph) if buy_graph else 0
                sell_count = sum(int(e[1]) for e in sell_graph) if sell_graph else 0

                sell_cents = histogram.get("lowest_sell_order")
                sell_price = int(sell_cents) / 100.0 if sell_cents else None

                if item_obj:
                    item_obj.buy_order_price_usd = c.buy_order_price
                    item_obj.buy_order_count = buy_count
                    item_obj.sell_order_price_usd = sell_price
                    item_obj.sell_order_count = sell_count
                    item_obj.updated_at = now

        # Phase 4: pricehistory
        history = await steam.get_price_history(c.hash_name, app_id=APP_ID)
        if history:
            rows_inserted = 0
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
                rows_inserted += 1

        if i % 5 == 0:
            await session.commit()
            logger.info(f"  Прогресс Steam: {i}/{enrich_total} ({i*100//enrich_total}%)")

    await session.commit()
    logger.info(f"Шаг 3 завершён: загружено {enrich_total} новых, {total - enrich_total} из кэша")


# ──────────────────────────────────────────────────────
# Шаг 4: Верификация кандидатов
# ──────────────────────────────────────────────────────

async def step4_verify(
    session: AsyncSession,
    candidates: list[Candidate],
    already_bought_counts: dict[str, int] | None = None,
) -> list[Candidate]:
    """Верифицировать кандидатов по истории и ценам.

    Критерии:
    - >= MIN_MONTHLY_SALES продаж за 30 дней
    - Текущая мин. цена Steam соответствует исторической
    - Цена Steam >= исторической медиане
    """
    already_bought_counts = already_bought_counts or {}
    logger.info(f"Шаг 4: Верификация кандидатов (мин. {MIN_MONTHLY_SALES} продаж/мес)...")
    verified: list[Candidate] = []

    for c in candidates:
        # Суммарные продажи за последние 30 дней
        result = await session.execute(
            select(
                func.coalesce(func.sum(SteamSalesHistory.volume), 0),
                func.count(func.distinct(func.substr(SteamSalesHistory.sale_date, 1, 10))),
            )
            .where(SteamSalesHistory.hash_name == c.hash_name)
            .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
        )
        row = result.one_or_none()
        total_volume = row[0] if row else 0
        days_with_sales = row[1] if row else 0

        # Продажи в день (для отчёта)
        if days_with_sales > 0:
            c.daily_sales = total_volume / max(days_with_sales, 1)
        else:
            c.daily_sales = 0

        if total_volume < MIN_MONTHLY_SALES:
            logger.debug(
                f"  ОТКЛОНЁН (мало продаж: {total_volume}/мес): {c.hash_name}"
            )
            continue

        # Медианная цена из истории за 30 дней
        price_result = await session.execute(
            select(SteamSalesHistory.price_usd)
            .where(SteamSalesHistory.hash_name == c.hash_name)
            .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
            .order_by(SteamSalesHistory.price_usd)
        )
        history_prices = [row[0] for row in price_result.all()]

        if history_prices:
            mid = len(history_prices) // 2
            hist_median = history_prices[mid]

            # Текущая цена Steam должна быть <= исторической медианы * 1.2
            # (цена не завышена сильно выше нормы)
            if c.steam_price > hist_median * 1.5:
                logger.debug(
                    f"  ОТКЛОНЁН (цена Steam ${c.steam_price:.2f} >> "
                    f"историческая ${hist_median:.2f}): {c.hash_name}"
                )
                continue

            # Цена Steam >= историческая медиана * 0.8
            # (цена не провалилась, можно продать)
            if c.steam_price < hist_median * 0.5:
                logger.debug(
                    f"  ОТКЛОНЁН (цена Steam ${c.steam_price:.2f} << "
                    f"историческая ${hist_median:.2f}): {c.hash_name}"
                )
                continue

            c.history_confirms_price = True

        c.verified = True

        # Расчёт количества: продать за 2-3 дня после трейд-бана
        # Берём daily_sales * 2 (продадим за ~2 дня если ставим по рынку)
        # Но не больше MAX_QTY_PER_ITEM и не больше наличия на CS2DT
        already_have = already_bought_counts.get(c.hash_name, 0)
        remaining_allowed = max(0, MAX_QTY_PER_ITEM - already_have)

        if remaining_allowed == 0:
            continue  # Уже достигли лимита

        if c.daily_sales and c.daily_sales > 0:
            safe_qty = max(1, int(c.daily_sales * 2))
            c.buy_quantity = min(safe_qty, remaining_allowed, c.cs2dt_quantity)
        else:
            c.buy_quantity = 1

        # Ограничение по бюджету на один предмет
        max_by_budget = max(1, int(MAX_SPEND_PER_ITEM / c.cs2dt_price))
        c.buy_quantity = min(c.buy_quantity, max_by_budget)

        verified.append(c)

    logger.info(f"Шаг 4: Верифицировано {len(verified)} из {len(candidates)} кандидатов")

    total_potential = sum(c.buy_quantity * c.cs2dt_price for c in verified)
    logger.info(f"  Потенциальный объём покупок: ${total_potential:.2f}")

    for c in verified:
        logger.info(
            f"  ✓ {c.profit_pct:>6.1f}%  x{c.buy_quantity:<2}  "
            f"CS2DT ${c.cs2dt_price:.2f} → Steam ${c.steam_price:.2f}  "
            f"продаж/день={c.daily_sales:.1f}  vol24h={c.volume_24h}  "
            f"{c.hash_name[:50]}"
        )

    return verified


# ──────────────────────────────────────────────────────
# Шаг 5: Покупка на CS2DT
# ──────────────────────────────────────────────────────

async def step5_buy(
    cs2dt: CS2DTClient,
    verified: list[Candidate],
) -> list[dict]:
    """Купить верифицированные предметы на CS2DT.

    Сортирует по (profit_pct * daily_sales) для максимизации прибыли.
    Покупает в рамках доступного баланса.
    """
    if not verified:
        logger.info("Шаг 5: Нет верифицированных предметов для покупки")
        return []

    # Получить баланс
    balance_data = await cs2dt.get_balance()
    balance = float(balance_data.get("data", "0"))
    logger.info(f"Шаг 5: Баланс CS2DT: ${balance:.2f}")

    if balance <= 0:
        logger.warning("Баланс CS2DT = $0, покупки невозможны")
        return []

    # Сортировка: score = profit_pct * daily_sales (ликвидные и прибыльные первыми)
    scored = sorted(
        verified,
        key=lambda c: (c.profit_pct * (c.daily_sales or 0)),
        reverse=True,
    )

    purchases: list[dict] = []
    remaining = balance
    daily_spent = 0.0

    for c in scored:
        if daily_spent >= DAILY_TARGET:
            logger.info(f"  Достигнут дневной лимит ${DAILY_TARGET:.0f}")
            break

        if remaining < c.cs2dt_price:
            logger.info(f"  Недостаточно баланса (${remaining:.2f}) для {c.hash_name}")
            continue

        # Перепроверить цену на CS2DT перед покупкой
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

            # Цена не должна быть выше, чем при анализе
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

        # Сколько покупаем — min(запланировано, наличие, по балансу, по дневному лимиту)
        qty = min(
            c.buy_quantity,
            fresh_qty,
            max(1, int(remaining / actual_price)),
            max(1, int((DAILY_TARGET - daily_spent) / actual_price)),
        )

        logger.info(
            f"  ПОКУПКА: {c.hash_name} x{qty} "
            f"за ${actual_price:.2f}/шт (прибыль {c.profit_pct:.1f}%, "
            f"продаж/день={c.daily_sales:.1f})"
        )

        # Покупаем qty штук по одной через quick_buy
        bought_count = 0
        for unit in range(qty):
            out_trade_no = str(uuid.uuid4())

            try:
                result = await cs2dt.quick_buy(
                    out_trade_no=out_trade_no,
                    trade_url=TRADE_URL,
                    max_price=actual_price,
                    market_hash_name=c.hash_name,
                    app_id=APP_ID,
                    low_price=True,
                )

                buy_price = result.get("buyPrice", actual_price)
                order_id = result.get("orderId", "")
                delivery = result.get("delivery", 0)

                remaining -= float(buy_price)
                daily_spent += float(buy_price)
                bought_count += 1

                purchase_info = {
                    "hash_name": c.hash_name,
                    "buy_price": float(buy_price),
                    "order_id": order_id,
                    "out_trade_no": out_trade_no,
                    "delivery": delivery,
                    "steam_price": c.steam_price,
                    "profit_pct": c.profit_pct,
                    "daily_sales": c.daily_sales,
                    "unit": unit + 1,
                    "total_units": qty,
                }
                purchases.append(purchase_info)

            except CS2DTAPIError as e:
                logger.error(f"  ✗ Ошибка покупки {c.hash_name} (шт {unit+1}/{qty}): {e}")
                break  # Прерываем покупку этого предмета

            if remaining < actual_price:
                break

        if bought_count > 0:
            logger.info(
                f"  ✓ КУПЛЕНО: {c.hash_name} x{bought_count} "
                f"за ${actual_price * bought_count:.2f}"
            )

    unique_items = len({p["hash_name"] for p in purchases})
    logger.info(f"\nШаг 5 завершён: куплено {len(purchases)} шт ({unique_items} уникальных)")
    logger.info(f"  Потрачено: ${balance - remaining:.2f}")
    logger.info(f"  Остаток: ${remaining:.2f}")

    return purchases


# ──────────────────────────────────────────────────────
# Сохранить результаты покупок в БД
# ──────────────────────────────────────────────────────

async def save_purchases(session: AsyncSession, purchases: list[dict]) -> None:
    """Сохранить покупки в таблицу arbitrage_purchases (PostgreSQL)."""
    now = datetime.now(timezone.utc).isoformat()
    for p in purchases:
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
    logger.info(f"Покупки сохранены в arbitrage_purchases ({len(purchases)} записей)")


# ──────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────

async def main(skip_steam: bool = False) -> None:
    setup_logging("INFO")
    logger.info("═══════════════════════════════════════════════════")
    logger.info("  АРБИТРАЖ CS2DT → Steam Market (Dota 2)")
    logger.info("═══════════════════════════════════════════════════")
    logger.info(f"  Параметры:")
    logger.info(f"    Мин. лотов Steam:     {MIN_STEAM_LISTINGS}")
    logger.info(f"    Мин. цена Steam:      ${MIN_STEAM_PRICE}")
    logger.info(f"    Мин. прибыль:         {MIN_PROFIT_PCT}%")
    logger.info(f"    Макс. цена CS2DT:     ${MAX_CS2DT_PRICE}")
    logger.info(f"    Мин. продаж/мес:      {MIN_MONTHLY_SALES}")
    logger.info(f"    Макс. штук/предмет:   {MAX_QTY_PER_ITEM}")
    logger.info(f"    Макс. $/предмет:      ${MAX_SPEND_PER_ITEM}")
    logger.info(f"    Дневной таргет:       ${DAILY_TARGET}")
    logger.info(f"    Комиссия Steam:       {STEAM_FEE_RATE*100:.0f}%")
    logger.info(f"    Trade URL:            {TRADE_URL[:40]}...")
    logger.info("═══════════════════════════════════════════════════")

    if not TRADE_URL or "partner=" not in TRADE_URL:
        logger.error("TRADE_URL не задан в .env — покупки невозможны")
        return

    await init_db()

    cs2dt = CS2DTClient()
    steam = SteamClient()

    try:
        async with async_session() as session:
            # Шаг 1: Фильтр из БД
            steam_items = await step1_filter_items(session)
            if not steam_items:
                logger.error("Нет подходящих предметов в БД. Запустите scan_steam_dota2.py")
                return

            # Загрузить количество уже купленных предметов (за последние 7 дней)
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
            total_prev = sum(already_bought_counts.values())
            logger.info(
                f"Куплено за 7 дней: {total_prev} шт "
                f"({len(already_bought_counts)} уникальных)"
            )

            # Не исключаем полностью — в step2 пропускаем только если уже >= MAX_QTY_PER_ITEM
            already_purchased = {
                name for name, count in already_bought_counts.items()
                if count >= MAX_QTY_PER_ITEM
            }
            if already_purchased:
                logger.info(f"  Достигли лимита {MAX_QTY_PER_ITEM} шт: {len(already_purchased)} предметов")

            # Шаг 2: Сравнение с CS2DT
            candidates = await step2_compare_cs2dt(cs2dt, steam_items, already_purchased)
            if not candidates:
                logger.info("Нет кандидатов с достаточной прибылью")
                return

            # Шаг 3: Детальные данные Steam
            if skip_steam:
                logger.info("Шаг 3: --skip-steam — используем только кэшированные данные Steam")
                # Загружаем данные из БД для кэшированных кандидатов
                enriched_candidates = []
                for c in candidates:
                    result = await session.execute(
                        select(SteamItem.volume_24h, SteamItem.buy_order_price_usd)
                        .where(SteamItem.hash_name == c.hash_name)
                    )
                    row = result.one_or_none()
                    if row and row[0] is not None:
                        c.volume_24h = row[0]
                        c.buy_order_price = row[1]
                        enriched_candidates.append(c)
                logger.info(
                    f"  Из {len(candidates)} кандидатов {len(enriched_candidates)} имеют кэш Steam"
                )
                candidates = enriched_candidates
            else:
                await step3_enrich_steam(steam, session, candidates)

            # Шаг 4: Верификация
            verified = await step4_verify(session, candidates, already_bought_counts)
            if not verified:
                logger.info("Ни один кандидат не прошёл верификацию")
                return

            # Шаг 5: Покупка
            purchases = await step5_buy(cs2dt, verified)

            # Сохранить
            if purchases:
                await save_purchases(session, purchases)

    finally:
        await steam.close()
        await cs2dt.close()

    logger.info("═══════════════════════════════════════════════════")
    logger.info("  АРБИТРАЖ ЗАВЕРШЁН")
    logger.info("═══════════════════════════════════════════════════")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Арбитраж CS2DT → Steam")
    parser.add_argument("--skip-steam", action="store_true",
                        help="Пропустить обогащение Steam, использовать кэш")
    args = parser.parse_args()
    asyncio.run(main(skip_steam=args.skip_steam))
