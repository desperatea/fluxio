"""Тест CS2DT клиента из bot/api/cs2dt_client.py."""

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Добавляем корень проекта в path
sys.path.insert(0, ".")

from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError


async def main() -> None:
    client = CS2DTClient()
    try:
        # 1. Баланс
        print("=== Баланс ===")
        bal = await client.get_balance()
        print(f"  userId: {bal.get('userId')}")
        print(f"  amount: ${bal.get('data')}")

        # 2. Курс
        print("\n=== Курс USD/CNY ===")
        rate = await client.get_usd_cny_rate()
        print(f"  rate: {rate}")

        # 3. Поиск Dota 2
        print("\n=== Рынок Dota 2 (топ-5 дешёвых) ===")
        market = await client.search_market(app_id=570, page=1, limit=5, order_by=1)
        for item in market.get("list", [])[:5]:
            pi = item.get("priceInfo", {})
            print(f"  {item.get('marketHashName')} — ${pi.get('price')} (qty: {pi.get('quantity')})")

        # 4. Листинги первого предмета
        if market.get("list"):
            first = market["list"][0]
            item_id = first.get("itemId")
            print(f"\n=== Листинги: {first.get('itemName')} (itemId={item_id}) ===")
            listings = await client.get_sell_list(item_id, limit=3, order_by=2)
            for s in listings.get("list", [])[:3]:
                print(f"  productId={s.get('id')} | ${s.get('price')} | "
                      f"delivery={s.get('delivery')}")

        # 5. Цены пакетом
        print("\n=== Цены пакетом ===")
        prices = await client.get_prices_batch(
            ["Saw of the Tree Punisher", "Inscribed Shatterblast Crown"],
            app_id=570,
        )
        for p in (prices if isinstance(prices, list) else []):
            print(f"  {p.get('marketHashName')} — ${p.get('price')} "
                  f"(avg: ${p.get('avgPrice')}, median: ${p.get('medianPrice')})")

        # 6. Заказы
        print("\n=== Заказы покупателя ===")
        try:
            orders = await client.get_buyer_orders(app_id=570, limit=3)
            for o in orders.get("list", [])[:3]:
                print(f"  orderId={o.get('orderId')} | {o.get('statusName')} | ${o.get('price')}")
            if not orders.get("list"):
                print("  (нет заказов)")
        except CS2DTAPIError as e:
            print(f"  Ошибка: {e}")

        print("\nВсе тесты пройдены!")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
