"""Тест CS2DT API — баланс, поиск рынка, листинги."""

import asyncio
import json
import os
import ssl
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://openapi.cs2dt.com"
APP_KEY = os.getenv("CS2DT_APP_KEY", "")


async def request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    params: dict | None = None,
    json_body: dict | None = None,
    label: str = "",
) -> dict | None:
    """Выполнить запрос к CS2DT API."""
    query = {"app-key": APP_KEY}
    if params:
        query.update(params)

    print(f"\n--- {label or path} ---")
    try:
        async with session.request(method, path, params=query, json=json_body) as resp:
            raw = await resp.text()
            if resp.status == 429 or "Too Many" in raw[:50]:
                print(f"  Rate limit ({resp.status}), ждём 3с...")
                await asyncio.sleep(3)
                return None

            data = json.loads(raw)
            print(f"  HTTP {resp.status} | success={data.get('success')}")

            if not data.get("success"):
                print(f"  errorCode: {data.get('errorCode')}")
                print(f"  errorMsg: {data.get('errorMsg')}")
                return None

            return data.get("data")
    except Exception as e:
        print(f"  ОШИБКА: {e}")
        return None


async def main() -> None:
    if not APP_KEY:
        print("CS2DT_APP_KEY не найден в .env!")
        return

    masked = f"{APP_KEY[:6]}***{APP_KEY[-4:]}"
    print(f"CS2DT API тест | Ключ: {masked}")
    print(f"Base URL: {BASE_URL}")

    ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        base_url=BASE_URL, timeout=timeout, connector=connector
    ) as session:

        # 1. Баланс
        balance = await request(session, "GET", "/v2/user/v1/t-coin/balance", label="Баланс")
        if balance:
            print(f"  userId: {balance.get('userId')}")
            print(f"  name: {balance.get('name')}")
            print(f"  amount: {balance.get('data')}")

        await asyncio.sleep(1)

        # 2. Курс USD/CNY
        rate = await request(session, "GET", "/v1/currency/rate/usd-cny", label="Курс USD/CNY")
        if rate is not None:
            print(f"  rate: {rate}")

        await asyncio.sleep(1)

        # 3. Поиск рынка Dota 2
        dota_market = await request(
            session, "GET", "/v2/product/v2/search",
            params={"appId": 570, "page": 1, "limit": 5},
            label="Рынок Dota 2 (appId=570)",
        )
        if dota_market:
            items = dota_market.get("list", [])
            print(f"  total: {dota_market.get('total')}, pages: {dota_market.get('pages')}")
            for item in items[:3]:
                pi = item.get("priceInfo", {})
                print(f"    - {item.get('itemName', '?')} | "
                      f"${pi.get('price', '?')} | "
                      f"qty: {pi.get('quantity', '?')} | "
                      f"itemId: {item.get('itemId')}")

        await asyncio.sleep(1)

        # 4. Поиск рынка CS2
        cs2_market = await request(
            session, "GET", "/v2/product/v2/search",
            params={"appId": 730, "page": 1, "limit": 5},
            label="Рынок CS2 (appId=730)",
        )
        if cs2_market:
            items = cs2_market.get("list", [])
            print(f"  total: {cs2_market.get('total')}, pages: {cs2_market.get('pages')}")
            for item in items[:3]:
                pi = item.get("priceInfo", {})
                print(f"    - {item.get('itemName', '?')} | "
                      f"${pi.get('price', '?')} | "
                      f"qty: {pi.get('quantity', '?')} | "
                      f"itemId: {item.get('itemId')}")

        await asyncio.sleep(1)

        # 5. Проверка Steam аккаунта
        trade_url = os.getenv("TRADE_URL", "").strip("'\"")
        if trade_url:
            steam_check = await request(
                session, "GET", "/v2/user/steam-info",
                params={"type": 1, "appId": 730, "tradeUrl": trade_url},
                label="Steam аккаунт (проверка для покупки)",
            )
            if steam_check:
                print(f"  checkStatus: {steam_check.get('checkStatus')} (0=checking, 1=ok, 2=problem)")
                si = steam_check.get("steamInfo", {})
                print(f"  nickName: {si.get('nickName')}")
                print(f"  steamId: {si.get('steamId')}")

        await asyncio.sleep(1)

        # 6. Заказы покупателя
        orders = await request(
            session, "POST", "/v2/order/buyer/v2/list",
            params={"appId": 730, "page": 1, "limit": 5},
            label="Заказы покупателя (CS2)",
        )
        if orders:
            order_list = orders.get("list", [])
            print(f"  total: {orders.get('total')}")
            for o in order_list[:3]:
                print(f"    - orderId: {o.get('orderId')} | "
                      f"status: {o.get('statusName')} | "
                      f"price: ${o.get('price')} | "
                      f"item: {o.get('name', '?')}")

    print("\n" + "=" * 60)
    print("Тест завершён!")


if __name__ == "__main__":
    asyncio.run(main())
