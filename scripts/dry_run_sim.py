"""Dry-run симуляция арбитража: CS2DT → Steam Market (Dota 2).

Проходит все шаги анализа (фильтр, сравнение цен, верификация),
но НЕ покупает. Показывает что бы мы купили и на какую сумму.
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
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.db.models import SteamItem, SteamSalesHistory, ArbitragePurchase

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


async def main() -> None:
    print("=" * 80)
    print("  DRY-RUN СИМУЛЯЦИЯ АРБИТРАЖА CS2DT → Steam (Dota 2)")
    print("=" * 80)
    print(f"  Мин. лотов Steam:   {MIN_STEAM_LISTINGS}")
    print(f"  Мин. цена Steam:    ${MIN_STEAM_PRICE}")
    print(f"  Мин. прибыль:       {MIN_PROFIT_PCT}%")
    print(f"  Макс. цена CS2DT:   ${MAX_CS2DT_PRICE}")
    print(f"  Мин. продаж/мес:    {MIN_MONTHLY_SALES}")
    print(f"  Комиссия Steam:     {STEAM_FEE_RATE*100:.0f}%")
    print("=" * 80)

    await init_db()

    async with async_session() as session:
        # === Шаг 1: Фильтр из БД ===
        result = await session.execute(
            select(SteamItem.hash_name, SteamItem.sell_price_usd, SteamItem.sell_listings)
            .where(SteamItem.sell_listings >= MIN_STEAM_LISTINGS)
            .where(SteamItem.sell_price_usd >= MIN_STEAM_PRICE)
            .order_by(SteamItem.sell_listings.desc())
        )
        steam_items = result.all()

        # Уже купленные за 7 дней
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

    prev_total = sum(already_bought_counts.values())
    print(f"\nШаг 1: {len(steam_items)} предметов в БД (>={MIN_STEAM_LISTINGS} лотов, >={MIN_STEAM_PRICE}$)")
    print(f"  Уже куплено за 7д: {prev_total} шт ({len(already_bought_counts)} уникальных)")

    # === Шаг 2: Сравнение с CS2DT (свежие цены) ===
    cs2dt = CS2DTClient()
    try:
        balance_data = await cs2dt.get_balance()
        balance = float(balance_data.get("data", "0"))
        print(f"  Баланс CS2DT: ${balance:.2f}")

        hash_names = [item[0] for item in steam_items]
        cs2dt_prices: dict[str, dict] = {}

        print(f"\n  Запрос свежих цен CS2DT ({len(hash_names)} предметов)...")
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
                print(f"  Ошибка CS2DT batch {i}: {e}")
                continue

        print(f"  CS2DT вернул цены для {len(cs2dt_prices)} предметов")

        # Поиск кандидатов
        steam_map = {item[0]: item for item in steam_items}
        candidates: list[dict] = []

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
            steam_after_fee = steam_price * (1 - STEAM_FEE_RATE)
            profit = steam_after_fee - cs2dt_price
            profit_pct = (profit / cs2dt_price) * 100
            if profit_pct >= MIN_PROFIT_PCT:
                candidates.append({
                    "hash_name": hash_name,
                    "steam_price": steam_price,
                    "steam_listings": steam_listings,
                    "cs2dt_price": cs2dt_price,
                    "cs2dt_quantity": cs2dt_qty,
                    "profit": round(profit, 4),
                    "profit_pct": round(profit_pct, 2),
                })

        candidates.sort(key=lambda c: c["profit_pct"], reverse=True)
        print(f"\nШаг 2: {len(candidates)} кандидатов (прибыль >= {MIN_PROFIT_PCT}%, цена <= ${MAX_CS2DT_PRICE})")

        if candidates:
            print(f"\n  Топ-20 по прибыли (до верификации):")
            for i, c in enumerate(candidates[:20], 1):
                print(
                    f"    {i:>2}. {c['profit_pct']:>7.1f}%  "
                    f"CS2DT ${c['cs2dt_price']:.2f} -> Steam ${c['steam_price']:.2f}  "
                    f"[{c['cs2dt_quantity']}/{c['steam_listings']}]  "
                    f"{c['hash_name'][:50]}"
                )
            if len(candidates) > 20:
                print(f"    ... и ещё {len(candidates) - 20}")

        # === Шаг 3+4: Верификация из кэша ===
        print(f"\nШаг 3-4: Верификация из кэша Steam...")

        async with async_session() as session:
            verified: list[dict] = []
            reject_reasons: dict[str, int] = {}

            for c in candidates:
                hn = c["hash_name"]

                # --- Источник 1: история продаж (приоритет) ---
                result = await session.execute(
                    select(
                        func.coalesce(func.sum(SteamSalesHistory.volume), 0),
                        func.count(func.distinct(func.substr(SteamSalesHistory.sale_date, 1, 10))),
                    )
                    .where(SteamSalesHistory.hash_name == hn)
                    .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
                )
                hist_row = result.one_or_none()
                hist_volume = hist_row[0] if hist_row else 0
                hist_days = hist_row[1] if hist_row else 0
                has_history = hist_volume > 0

                # --- Источник 2: priceoverview (фоллбэк) ---
                result = await session.execute(
                    select(SteamItem.volume_24h, SteamItem.median_price_usd, SteamItem.buy_order_price_usd)
                    .where(SteamItem.hash_name == hn)
                )
                row = result.one_or_none()
                volume_24h = (row[0] or 0) if row else 0
                median_price = (row[1] or 0.0) if row else 0.0
                c["volume_24h"] = volume_24h
                c["buy_order_price"] = row[2] if row else None

                if not has_history and volume_24h == 0:
                    reject_reasons["нет данных"] = reject_reasons.get("нет данных", 0) + 1
                    continue

                # --- Определяем объём продаж ---
                if has_history:
                    total_volume = hist_volume
                    daily_sales = hist_volume / max(hist_days, 1) if hist_days > 0 else 0.0
                    c["data_source"] = "история"
                else:
                    total_volume = volume_24h * 30
                    daily_sales = float(volume_24h)
                    c["data_source"] = "overview"

                c["total_sales_30d"] = total_volume
                c["daily_sales"] = daily_sales

                if total_volume < MIN_MONTHLY_SALES:
                    reject_reasons["мало продаж"] = reject_reasons.get("мало продаж", 0) + 1
                    continue

                # --- Проверка цены ---
                if has_history:
                    result = await session.execute(
                        select(SteamSalesHistory.price_usd)
                        .where(SteamSalesHistory.hash_name == hn)
                        .where(SteamSalesHistory.sale_date >= text("(NOW() - INTERVAL '30 days')::text"))
                        .order_by(SteamSalesHistory.price_usd)
                    )
                    history_prices = [r[0] for r in result.all()]
                    if history_prices:
                        hist_median = history_prices[len(history_prices) // 2]
                        if c["steam_price"] > hist_median * 1.5:
                            reject_reasons["цена >> истории"] = reject_reasons.get("цена >> истории", 0) + 1
                            continue
                        if c["steam_price"] < hist_median * 0.5:
                            reject_reasons["цена << истории"] = reject_reasons.get("цена << истории", 0) + 1
                            continue
                elif median_price > 0:
                    if c["steam_price"] > median_price * 1.5:
                        reject_reasons["цена >> медианы"] = reject_reasons.get("цена >> медианы", 0) + 1
                        continue
                    if c["steam_price"] < median_price * 0.5:
                        reject_reasons["цена << медианы"] = reject_reasons.get("цена << медианы", 0) + 1
                        continue

                # Количество для покупки
                already_have = already_bought_counts.get(hn, 0)
                remaining_allowed = max(0, MAX_QTY_PER_ITEM - already_have)
                if remaining_allowed == 0:
                    reject_reasons["лимит шт достигнут"] = reject_reasons.get("лимит шт достигнут", 0) + 1
                    continue

                if daily_sales > 0:
                    safe_qty = max(1, int(daily_sales * 2))
                    buy_qty = min(safe_qty, remaining_allowed, c["cs2dt_quantity"])
                else:
                    buy_qty = 1

                max_by_budget = max(1, int(MAX_SPEND_PER_ITEM / c["cs2dt_price"]))
                buy_qty = min(buy_qty, max_by_budget)
                c["buy_quantity"] = buy_qty
                verified.append(c)

        # Причины отказа
        if reject_reasons:
            print(f"\n  Причины отклонения:")
            for reason, count in sorted(reject_reasons.items(), key=lambda x: -x[1]):
                print(f"    {reason}: {count}")

        # Сортировка: score = profit_pct * daily_sales
        verified.sort(key=lambda c: c["profit_pct"] * (c["daily_sales"] or 0), reverse=True)

        total_spend = sum(c["buy_quantity"] * c["cs2dt_price"] for c in verified)
        total_items = sum(c["buy_quantity"] for c in verified)
        total_profit = sum(c["buy_quantity"] * c["profit"] for c in verified)

        print(f"\n{'=' * 80}")
        print(f"  РЕЗУЛЬТАТ DRY-RUN СИМУЛЯЦИИ")
        print(f"{'=' * 80}")
        print(f"  Баланс CS2DT:            ${balance:.2f}")
        print(f"  Верифицированных:         {len(verified)} уникальных предметов")
        print(f"  Всего штук к покупке:     {total_items}")
        print(f"  Общая сумма:              ${total_spend:.2f}")
        print(f"  Ожидаемая прибыль:        +${total_profit:.2f}")
        if total_spend > 0:
            print(f"  ROI (все):                {total_profit/total_spend*100:.1f}%")
        print(f"{'=' * 80}")

        if not verified:
            print("\n  Нет верифицированных кандидатов для покупки.")
            return

        # Детальный список с учётом баланса
        print(f"\n  СПИСОК ПОКУПОК (сортировка: profit% * daily_sales):")
        header = (
            f"  {'#':>3}  {'Приб%':>7}  {'x':>3}  {'CS2DT':>7}  {'Steam':>7}  "
            f"{'Профит':>8}  {'Прод/д':>6}  {'30д':>5}  Предмет"
        )
        print(header)
        print(f"  {'---':>3}  {'-------':>7}  {'---':>3}  {'-------':>7}  {'-------':>7}  "
              f"{'--------':>8}  {'------':>6}  {'-----':>5}  {'----------':>10}")

        cumulative = 0.0
        affordable_count = 0
        affordable_items_count = 0
        affordable_spend = 0.0
        affordable_profit = 0.0

        for i, c in enumerate(verified, 1):
            item_cost = c["buy_quantity"] * c["cs2dt_price"]
            profit_total = c["buy_quantity"] * c["profit"]
            cumulative += item_cost
            can_afford = cumulative <= balance

            if can_afford:
                affordable_count += 1
                affordable_items_count += c["buy_quantity"]
                affordable_spend += item_cost
                affordable_profit += profit_total

            marker = "  " if can_afford else " $"
            print(
                f"{marker}{i:>3}  {c['profit_pct']:>6.1f}%  x{c['buy_quantity']:<2}  "
                f"${c['cs2dt_price']:>5.2f}  ${c['steam_price']:>5.2f}  "
                f"+${profit_total:>5.2f}  {c['daily_sales']:>5.1f}  "
                f"{c['total_sales_30d']:>5}  {c['hash_name'][:45]}"
            )

        print(f"\n  {'=' * 78}")
        print(f"  ИТОГО (в рамках баланса ${balance:.2f}):")
        print(f"    Можем купить:          {affordable_count} предметов, {affordable_items_count} штук")
        print(f"    Потратим:              ${affordable_spend:.2f}")
        print(f"    Ожидаемая прибыль:     +${affordable_profit:.2f}")
        if affordable_spend > 0:
            print(f"    ROI:                   {affordable_profit/affordable_spend*100:.1f}%")
        print(f"    Остаток баланса:       ${balance - affordable_spend:.2f}")
        print(f"  {'=' * 78}")

        # Если баланс не хватает на всё
        if total_spend > balance:
            print(f"\n  [!] Баланса не хватает на все предметы:")
            print(f"      Нужно ещё: ${total_spend - balance:.2f}")
            print(f"      Не купим:  {len(verified) - affordable_count} предметов, "
                  f"{total_items - affordable_items_count} штук")

    finally:
        await cs2dt.close()


if __name__ == "__main__":
    asyncio.run(main())
