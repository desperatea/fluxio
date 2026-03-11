"""Покупка дешёвого предмета Dota 2 через CS2DT API."""

import asyncio
import json
import os
import ssl
import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://openapi.cs2dt.com"
APP_KEY = os.getenv("CS2DT_APP_KEY", "")
TRADE_URL = os.getenv("TRADE_URL", "").strip("'\"")


async def api_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict | None:
    """Запрос к CS2DT API."""
    query = {"app-key": APP_KEY}
    if params:
        query.update(params)
    async with session.request(method, path, params=query, json=json_body) as resp:
        raw = await resp.text()
        data = json.loads(raw)
        return data


async def main() -> None:
    if not APP_KEY or not TRADE_URL:
        print("CS2DT_APP_KEY или TRADE_URL не найдены в .env!")
        return

    ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        base_url=BASE_URL, timeout=timeout, connector=connector
    ) as session:

        # 1. Проверяем баланс
        print("=== Баланс ===")
        bal = await api_request(session, "GET", "/v2/user/v1/t-coin/balance")
        if not bal or not bal.get("success"):
            print(f"Ошибка баланса: {bal}")
            return
        balance = float(bal["data"]["data"])
        print(f"  Баланс: ${balance}")

        await asyncio.sleep(0.5)

        # 2. Ищем самые дешёвые предметы Dota 2
        print("\n=== Поиск дешёвых предметов Dota 2 ===")
        search = await api_request(
            session, "GET", "/v2/product/v2/search",
            params={
                "appId": 570,
                "orderBy": 1,  # цена по возрастанию
                "page": 1,
                "limit": 10,
            },
        )
        if not search or not search.get("success"):
            print(f"Ошибка поиска: {search}")
            return

        items = search["data"].get("list", [])
        print(f"  Найдено всего: {search['data'].get('total')}")
        print()

        for i, item in enumerate(items):
            pi = item.get("priceInfo", {})
            print(f"  [{i}] {item.get('itemName')} ({item.get('marketHashName')})")
            print(f"      цена: ${pi.get('price')} | qty: {pi.get('quantity')} | itemId: {item.get('itemId')}")
            print(f"      autoDeliver: ${pi.get('autoDeliverPrice')} (qty: {pi.get('autoDeliverQuantity')})")
            print()

        if not items:
            print("Нет предметов!")
            return

        # 3. Берём самый дешёвый предмет и смотрим его листинги
        cheapest = items[0]
        item_id = cheapest.get("itemId")
        item_name = cheapest.get("itemName")
        item_price = cheapest.get("priceInfo", {}).get("price")

        print(f"=== Листинги для: {item_name} (itemId={item_id}) ===")
        await asyncio.sleep(0.5)

        listings = await api_request(
            session, "GET", "/v2/product/v1/sell/list",
            params={
                "itemId": item_id,
                "page": 1,
                "limit": 5,
                "orderBy": 2,  # цена по возрастанию
            },
        )
        if not listings or not listings.get("success"):
            print(f"Ошибка листингов: {listings}")
            return

        sell_list = listings["data"].get("list", [])
        for s in sell_list[:5]:
            print(f"  productId: {s.get('id')} | ${s.get('price')} | "
                  f"delivery: {s.get('delivery')} (1=manual,2=auto) | "
                  f"seller: {s.get('sellerInfo', {}).get('nickName', '?')}")

        if not sell_list:
            print("Нет листингов!")
            return

        # 4. Покупаем самый дешёвый лот
        target = sell_list[0]
        product_id = target["id"]
        buy_price = float(target["price"])

        print(f"\n=== ПОКУПКА ===")
        print(f"  Предмет: {item_name}")
        print(f"  productId: {product_id}")
        print(f"  Цена: ${buy_price}")
        print(f"  TradeURL: {TRADE_URL[:60]}...")

        out_trade_no = str(uuid.uuid4())
        print(f"  outTradeNo: {out_trade_no}")

        await asyncio.sleep(0.5)

        buy_result = await api_request(
            session, "POST", "/v2/trade/v2/buy",
            json_body={
                "outTradeNo": out_trade_no,
                "tradeUrl": TRADE_URL,
                "productId": int(product_id),
                "maxPrice": buy_price,
            },
        )

        print(f"\n  Ответ API:")
        print(f"  success: {buy_result.get('success')}")
        if buy_result.get("success"):
            d = buy_result.get("data", {})
            print(f"  orderId: {d.get('orderId')}")
            print(f"  buyPrice: ${d.get('buyPrice')}")
            print(f"  delivery: {d.get('delivery')} (1=manual,2=auto)")
            print(f"  offerId: {d.get('offerId')}")
        else:
            print(f"  errorCode: {buy_result.get('errorCode')}")
            print(f"  errorMsg: {buy_result.get('errorMsg')}")
            print(f"  errorData: {buy_result.get('errorData')}")

    print("\nГотово!")


if __name__ == "__main__":
    asyncio.run(main())
