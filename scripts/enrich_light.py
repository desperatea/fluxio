"""Лёгкое обогащение кандидатов: только priceoverview + pricehistory.

Пропускает nameid и histogram (ордера) — они вызывают 429 от Steam.
Для верификации достаточно priceoverview (цена, volume) + pricehistory (продажи за 30д).

Запуск:
    python scripts/enrich_light.py
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from loguru import logger
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helper import async_session, init_db
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.api.steam_client import SteamClient
from fluxio.db.models import SteamItem, SteamSalesHistory, ArbitragePurchase
from fluxio.utils.logger import setup_logging

APP_ID = 570

# Параметры арбитража
MIN_STEAM_LISTINGS = 10
MIN_STEAM_PRICE = 0.09
MIN_PROFIT_PCT = 45.0
STEAM_FEE_RATE = 0.15
MAX_CS2DT_PRICE = 0.80
MIN_MONTHLY_SALES = 60
MAX_QTY_PER_ITEM = 15
MAX_SPEND_PER_ITEM = 5.00


@dataclass
class Candidate:
    hash_name: str
    steam_price: float
    steam_listings: int
    cs2dt_price: float
    cs2dt_quantity: int
    profit: float
    profit_pct: float
    # После обогащения
    median_price: float | None = None
    lowest_price: float | None = None
    volume_24h: int | None = None
    daily_sales: float | None = None
    total_sales_30d: int = 0
    verified: bool = False
    buy_quantity: int = 1


async def step1_filter(session: AsyncSession) -> list[tuple]:
    """Фильтр из PostgreSQL: предметы с достаточным числом лотов и ценой."""
    result = await session.execute(
        select(SteamItem.hash_name, SteamItem.sell_price_usd, SteamItem.sell_listings)
        .where(SteamItem.sell_listings >= MIN_STEAM_LISTINGS)
        .where(SteamItem.sell_price_usd >= MIN_STEAM_PRICE)
        .order_by(SteamItem.sell_listings.desc())
    )
    items = result.all()
    logger.info(f"Шаг 1: {len(items)} предметов (>={MIN_STEAM_LISTINGS} лотов, >=${MIN_STEAM_PRICE}$)")
    return items


async def step2_compare(cs2dt: CS2DTClient, steam_items: list[tuple], skip: set[str]) -> list[Candidate]:
    """Сравнить цены CS2DT и Steam, найти кандидатов."""
    hash_names = [item[0] for item in steam_items]
    cs2dt_prices: dict[str, dict] = {}

    logger.info(f"Шаг 2: Запрос цен CS2DT ({len(hash_names)} предметов)...")
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
            logger.error(f"  Ошибка CS2DT batch {i}: {e}")
    logger.info(f"  CS2DT вернул цены для {len(cs2dt_prices)} предметов")

    steam_map = {item[0]: item for item in steam_items}
    candidates: list[Candidate] = []

    for hash_name, cs2dt_data in cs2dt_prices.items():
        if hash_name in skip:
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
    logger.info(f"Шаг 2: {len(candidates)} кандидатов (>={MIN_PROFIT_PCT}%, <={MAX_CS2DT_PRICE}$)")
    return candidates


async def step3_enrich_light(
    steam: SteamClient,
    session: AsyncSession,
    candidates: list[Candidate],
) -> None:
    """Лёгкое обогащение: только priceoverview + pricehistory (2 запроса на предмет).

    Пропускает nameid и histogram — они тяжёлые и часто получают 429.
    """
    # Определяем, кому нужно обогащение (только priceoverview)
    need_enrich: list[Candidate] = []
    for c in candidates:
        result = await session.execute(
            select(SteamItem.volume_24h, SteamItem.median_price_usd)
            .where(SteamItem.hash_name == c.hash_name)
        )
        row = result.one_or_none()
        if row and row[0] is not None and row[1] is not None:
            c.volume_24h = row[0]
            continue  # Уже есть priceoverview
        need_enrich.append(c)

    total = len(candidates)
    cached = total - len(need_enrich)
    logger.info(
        f"Шаг 3: {cached} уже обогащены, нужно загрузить: {len(need_enrich)}"
    )

    if not need_enrich:
        logger.info("Шаг 3: Все кандидаты обогащены, пропускаем")
        return

    now = datetime.now(timezone.utc).isoformat()
    enriched = 0
    failed = 0

    for i, c in enumerate(need_enrich, 1):
        # 1. priceoverview (1 запрос)
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
            enriched += 1
        else:
            failed += 1

        # pricehistory пропускаем — требует авторизацию, не работает через прокси
        # Используем volume_24h из priceoverview для верификации

        # Коммит и прогресс каждые 10 предметов
        if i % 10 == 0:
            await session.commit()
            pct = i * 100 // len(need_enrich)
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(now)).total_seconds()
            rate_per_min = i / max(elapsed, 1) * 60
            eta_min = (len(need_enrich) - i) / max(rate_per_min, 0.01)
            logger.info(
                f"  Прогресс: {i}/{len(need_enrich)} ({pct}%), "
                f"обогащено: {enriched}, ошибок: {failed}, "
                f"~{rate_per_min:.1f} предм/мин, ETA ~{eta_min:.0f} мин"
            )

    await session.commit()
    logger.info(
        f"Шаг 3 завершён: обогащено {enriched}, ошибок {failed}, "
        f"из кэша {cached}"
    )


async def step4_verify(
    session: AsyncSession,
    candidates: list[Candidate],
    already_bought_counts: dict[str, int],
) -> list[Candidate]:
    """Верификация кандидатов по истории продаж."""
    logger.info(f"Шаг 4: Верификация {len(candidates)} кандидатов...")
    verified: list[Candidate] = []
    reject_reasons: dict[str, int] = {}

    for c in candidates:
        # --- Источник 1: история продаж (приоритет) ---
        hist_result = await session.execute(
            select(
                func.coalesce(func.sum(SteamSalesHistory.volume), 0),
                func.count(func.distinct(func.substr(SteamSalesHistory.sale_date, 1, 10))),
            )
            .where(SteamSalesHistory.hash_name == c.hash_name)
            .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
        )
        hist_row = hist_result.one_or_none()
        hist_volume = hist_row[0] if hist_row else 0
        hist_days = hist_row[1] if hist_row else 0
        has_history = hist_volume > 0

        # --- Источник 2: priceoverview (фоллбэк) ---
        ov_result = await session.execute(
            select(SteamItem.volume_24h, SteamItem.median_price_usd)
            .where(SteamItem.hash_name == c.hash_name)
        )
        ov_row = ov_result.one_or_none()
        volume_24h = (ov_row[0] or 0) if ov_row else 0
        median_price = (ov_row[1] or 0.0) if ov_row else 0.0

        if not has_history and volume_24h == 0:
            reject_reasons["нет данных"] = reject_reasons.get("нет данных", 0) + 1
            continue

        # --- Определяем объём продаж ---
        if has_history:
            # Реальные данные за 30 дней
            c.total_sales_30d = hist_volume
            c.daily_sales = hist_volume / max(hist_days, 1) if hist_days > 0 else 0.0
        else:
            # Фоллбэк: экстраполяция volume_24h
            c.total_sales_30d = volume_24h * 30
            c.daily_sales = float(volume_24h)

        if c.total_sales_30d < MIN_MONTHLY_SALES:
            reject_reasons["мало продаж"] = reject_reasons.get("мало продаж", 0) + 1
            continue

        # --- Проверка цены ---
        if has_history:
            # Медиана из истории (надёжнее)
            price_result = await session.execute(
                select(SteamSalesHistory.price_usd)
                .where(SteamSalesHistory.hash_name == c.hash_name)
                .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
                .order_by(SteamSalesHistory.price_usd)
            )
            history_prices = [r[0] for r in price_result.all()]
            if history_prices:
                hist_median = history_prices[len(history_prices) // 2]
                if c.steam_price > hist_median * 1.5:
                    reject_reasons["цена >> истории"] = reject_reasons.get("цена >> истории", 0) + 1
                    continue
                if c.steam_price < hist_median * 0.5:
                    reject_reasons["цена << истории"] = reject_reasons.get("цена << истории", 0) + 1
                    continue
        elif median_price > 0:
            # Фоллбэк: median_price из priceoverview
            if c.steam_price > median_price * 1.5:
                reject_reasons["цена >> медианы"] = reject_reasons.get("цена >> медианы", 0) + 1
                continue
            if c.steam_price < median_price * 0.5:
                reject_reasons["цена << медианы"] = reject_reasons.get("цена << медианы", 0) + 1
                continue

        # Количество для покупки
        already_have = already_bought_counts.get(c.hash_name, 0)
        remaining_allowed = max(0, MAX_QTY_PER_ITEM - already_have)
        if remaining_allowed == 0:
            reject_reasons["лимит достигнут"] = reject_reasons.get("лимит достигнут", 0) + 1
            continue

        if c.daily_sales > 0:
            safe_qty = max(1, int(c.daily_sales * 2))
            c.buy_quantity = min(safe_qty, remaining_allowed, c.cs2dt_quantity)
        else:
            c.buy_quantity = 1

        max_by_budget = max(1, int(MAX_SPEND_PER_ITEM / c.cs2dt_price))
        c.buy_quantity = min(c.buy_quantity, max_by_budget)
        c.verified = True
        verified.append(c)

    if reject_reasons:
        logger.info(f"  Причины отклонения: {reject_reasons}")

    verified.sort(key=lambda c: c.profit_pct * (c.daily_sales or 0), reverse=True)

    total_spend = sum(c.buy_quantity * c.cs2dt_price for c in verified)
    total_items = sum(c.buy_quantity for c in verified)
    total_profit = sum(c.buy_quantity * c.profit for c in verified)

    logger.info(f"Шаг 4: Верифицировано {len(verified)} из {len(candidates)}")
    logger.info(f"  Штук к покупке: {total_items}")
    logger.info(f"  Сумма: ${total_spend:.2f}")
    logger.info(f"  Ожид. прибыль: +${total_profit:.2f}")
    if total_spend > 0:
        logger.info(f"  ROI: {total_profit/total_spend*100:.1f}%")

    for c in verified:
        logger.info(
            f"  ✓ {c.profit_pct:>6.1f}%  x{c.buy_quantity:<2}  "
            f"CS2DT ${c.cs2dt_price:.2f} → Steam ${c.steam_price:.2f}  "
            f"продаж/день={c.daily_sales:.1f}  30д={c.total_sales_30d}  "
            f"{c.hash_name[:50]}"
        )

    return verified


async def main() -> None:
    setup_logging("INFO")
    logger.info("=" * 60)
    logger.info("  ЛЁГКОЕ ОБОГАЩЕНИЕ (только priceoverview через прокси)")
    logger.info("  1 запрос на предмет, ~20 предм/мин")
    logger.info("=" * 60)

    await init_db()

    cs2dt = CS2DTClient()
    steam = SteamClient()

    try:
        async with async_session() as session:
            # Шаг 1
            steam_items = await step1_filter(session)
            if not steam_items:
                logger.error("Нет подходящих предметов")
                return

            # Баланс
            balance_data = await cs2dt.get_balance()
            balance = float(balance_data.get("data", "0"))
            logger.info(f"Баланс CS2DT: ${balance:.2f}")

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
                n for n, c in already_bought_counts.items() if c >= MAX_QTY_PER_ITEM
            }

            # Шаг 2
            candidates = await step2_compare(cs2dt, steam_items, already_purchased)
            if not candidates:
                logger.info("Нет кандидатов")
                return

            # С прокси можно быстрее — burst=1 чтобы не спамить
            from fluxio.utils.rate_limiter import TokenBucketRateLimiter
            steam._rate_limiter = TokenBucketRateLimiter(
                rate=0.33, burst=1, name="steam"
            )

            # Шаг 3 — лёгкое обогащение
            await step3_enrich_light(steam, session, candidates)

            # Шаг 4 — верификация
            verified = await step4_verify(session, candidates, already_bought_counts)

            if verified:
                logger.info("=" * 60)
                total_spend = sum(c.buy_quantity * c.cs2dt_price for c in verified)
                total_profit = sum(c.buy_quantity * c.profit for c in verified)
                logger.info(f"  ИТОГО: {len(verified)} предметов, "
                            f"{sum(c.buy_quantity for c in verified)} штук")
                logger.info(f"  Сумма: ${total_spend:.2f}, прибыль: +${total_profit:.2f}")
                if total_spend > 0:
                    logger.info(f"  ROI: {total_profit/total_spend*100:.1f}%")
                logger.info(f"  Баланс: ${balance:.2f} "
                            f"(хватит на ${min(total_spend, balance):.2f})")
                logger.info("=" * 60)

            logger.info("Покупка ПРОПУЩЕНА — только обогащение")

    finally:
        await steam.close()
        await cs2dt.close()

    logger.info("Обогащение завершено")


if __name__ == "__main__":
    asyncio.run(main())
