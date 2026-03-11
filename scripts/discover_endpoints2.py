"""Расширенный поиск эндпоинтов — без /v1/ и другие паттерны."""

from __future__ import annotations

import asyncio
import os
import ssl
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import aiohttp

BASE_URL = "https://openapi.c5game.com"
APP_KEY = os.getenv("C5GAME_APP_KEY", "")

# Больше вариантов — без /v1/, с /v2/, другие домены
CANDIDATES = {
    "product/sale list": [
        # Без версии
        ("GET", "/merchant/product/list"),
        ("POST", "/merchant/product/list"),
        ("GET", "/merchant/sale/list"),
        ("POST", "/merchant/sale/list"),
        ("GET", "/merchant/goods/list"),
        ("POST", "/merchant/goods/list"),
        # Другие паттерны
        ("GET", "/merchant/v1/product/list"),
        ("POST", "/merchant/v1/product/list"),
        ("GET", "/merchant/v1/sale/list"),
        ("POST", "/merchant/v1/sale/list"),
        ("POST", "/merchant/v1/goods/list"),
        # С appId как path param
        ("GET", "/merchant/product/570/list"),
        ("GET", "/merchant/sale/570/list"),
    ],
    "item/price": [
        ("GET", "/merchant/item/list"),
        ("POST", "/merchant/item/list"),
        ("GET", "/merchant/item/price"),
        ("POST", "/merchant/item/price"),
        ("GET", "/merchant/v1/item/list"),
        ("POST", "/merchant/v1/item/list"),
        ("POST", "/merchant/v1/item/price"),
        ("POST", "/merchant/v1/item/stat"),
    ],
    "buy": [
        ("POST", "/merchant/buy/quick"),
        ("POST", "/merchant/buy/normal"),
        ("POST", "/merchant/buy/batch"),
        ("POST", "/merchant/v1/buy/quick"),
        ("POST", "/merchant/v1/buy/normal"),
        ("POST", "/merchant/order/buy"),
        ("POST", "/merchant/order/v1/buy"),
        ("POST", "/merchant/order/v1/create"),
        ("POST", "/merchant/trade/buy"),
    ],
    "account": [
        ("GET", "/merchant/account/steam"),
        ("GET", "/merchant/account/info"),
        ("GET", "/merchant/account/v1/info"),
        ("GET", "/merchant/user/info"),
        ("GET", "/merchant/user/v1/info"),
        ("GET", "/merchant/v1/account/steam"),
        ("GET", "/merchant/v1/account/info"),
    ],
    "inventory": [
        ("GET", "/merchant/inventory/list"),
        ("GET", "/merchant/v1/inventory/list"),
        ("GET", "/merchant/steam/inventory"),
        ("GET", "/merchant/v1/steam/inventory"),
        ("GET", "/merchant/asset/list"),
    ],
    "order details": [
        ("GET", "/merchant/order/detail"),
        ("GET", "/merchant/order/v1/detail"),
        ("POST", "/merchant/order/cancel"),
        ("POST", "/merchant/order/v1/cancel"),
    ],
}


async def check(session, method, path):
    params = {"app-key": APP_KEY}
    try:
        async with session.request(
            method, path, params=params,
            json={} if method == "POST" else None,
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = await resp.text()
            return resp.status, data
    except Exception as e:
        return -1, str(e)


async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(
        base_url=BASE_URL, timeout=timeout, connector=connector,
    ) as session:
        for name, candidates in CANDIDATES.items():
            print(f"\n{'='*60}")
            print(f"  {name}")
            print(f"{'='*60}")

            for method, path in candidates:
                status, data = await check(session, method, path)
                if status == 404:
                    print(f"  {method:4} {path:45} -> 404")
                elif status == -1:
                    pass  # skip errors
                else:
                    print(f"  {method:4} {path:45} -> {status} !!!")
                    if isinstance(data, dict):
                        # Limit output
                        txt = str(data)
                        print(f"       {txt[:300]}")
                    else:
                        print(f"       {str(data)[:200]}")
                await asyncio.sleep(0.15)


if __name__ == "__main__":
    asyncio.run(main())
