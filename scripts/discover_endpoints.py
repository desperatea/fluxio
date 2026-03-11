"""Скрипт обнаружения правильных путей API C5Game.

Перебирает вероятные варианты эндпоинтов и проверяет какие отвечают не 404.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import aiohttp

BASE_URL = "https://openapi.c5game.com"
APP_KEY = os.getenv("C5GAME_APP_KEY", "")

# Вероятные паттерны путей для каждого эндпоинта
CANDIDATES = {
    "steam_info": [
        # GET варианты
        ("GET", "/merchant/account/v1/steam"),
        ("GET", "/merchant/account/v1/steam-info"),
        ("GET", "/merchant/steam/v1/info"),
        ("GET", "/merchant/user/v1/steam"),
        ("POST", "/merchant/account/v1/steam"),
    ],
    "product_list": [
        ("POST", "/merchant/product/v1/list"),
        ("POST", "/merchant/sale/v1/list"),
        ("POST", "/merchant/sale/v1/on-sale"),
        ("POST", "/merchant/goods/v1/list"),
        ("POST", "/merchant/market/v1/list"),
        ("POST", "/merchant/item/v1/list"),
        ("POST", "/merchant/item/v1/on-sale"),
        ("POST", "/merchant/listing/v1/list"),
        ("GET", "/merchant/product/v1/list"),
        ("GET", "/merchant/sale/v1/list"),
        ("GET", "/merchant/goods/v1/list"),
    ],
    "item_stat_batch": [
        ("POST", "/merchant/item/v1/stat/batch"),
        ("POST", "/merchant/item/v1/stats"),
        ("POST", "/merchant/item/v1/stat"),
        ("POST", "/merchant/product/v1/stat/batch"),
        ("POST", "/merchant/goods/v1/stat/batch"),
        ("GET", "/merchant/item/v1/stat/batch"),
    ],
    "item_price_batch": [
        ("POST", "/merchant/item/v1/price/batch"),
        ("POST", "/merchant/item/v1/prices"),
        ("POST", "/merchant/item/v1/price"),
        ("POST", "/merchant/product/v1/price/batch"),
        ("GET", "/merchant/item/v1/price/batch"),
    ],
    "buy_quick": [
        ("POST", "/merchant/buy/v1/quick"),
        ("POST", "/merchant/order/v1/buy/quick"),
        ("POST", "/merchant/order/v1/quick-buy"),
        ("POST", "/merchant/purchase/v1/quick"),
        ("POST", "/merchant/trade/v1/buy/quick"),
    ],
    "order_buyer_list": [
        ("POST", "/merchant/order/v1/buyer/list"),
        ("POST", "/merchant/order/v1/list"),
        ("GET", "/merchant/order/v1/buyer/list"),
        ("GET", "/merchant/order/v1/list"),
        ("POST", "/merchant/order/v1/buy/list"),
    ],
    "inventory_list": [
        ("GET", "/merchant/inventory/v1/list"),
        ("GET", "/merchant/steam/v1/inventory"),
        ("GET", "/merchant/asset/v1/list"),
        ("POST", "/merchant/inventory/v1/list"),
    ],
}


async def check_endpoint(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
) -> tuple[int, dict | str]:
    """Проверить один эндпоинт, вернуть (статус, ответ)."""
    params = {"app-key": APP_KEY}
    try:
        async with session.request(
            method,
            path,
            params=params,
            json={} if method == "POST" else None,
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = await resp.text()
            return resp.status, data
    except Exception as e:
        return -1, str(e)


async def main() -> None:
    """Перебрать все кандидаты."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(
        base_url=BASE_URL,
        timeout=timeout,
        connector=connector,
    ) as session:
        for endpoint_name, candidates in CANDIDATES.items():
            print(f"\n{'='*60}")
            print(f"  {endpoint_name}")
            print(f"{'='*60}")

            for method, path in candidates:
                status, data = await check_endpoint(session, method, path)

                if status == 404:
                    print(f"  {method:4} {path:45} -> 404")
                elif status == -1:
                    print(f"  {method:4} {path:45} -> ОШИБКА: {data[:60]}")
                else:
                    # Не 404 — нашли что-то!
                    print(f"  {method:4} {path:45} -> {status} OK")
                    if isinstance(data, dict):
                        # Показать ключи ответа
                        print(f"       Ответ: {data}")
                    else:
                        print(f"       Ответ: {str(data)[:200]}")

                # Небольшая задержка чтобы не перегружать API
                await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
