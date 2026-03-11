"""Сканируем все категории маркета и выводим ВСЕ результаты."""
import asyncio
import aiohttp
import ssl
import os
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from dotenv import load_dotenv

load_dotenv("e:/c5games/.env")

API_KEY = os.getenv("C5GAME_API_KEY", "")
BASE_URL = "https://openapi.c5game.com"


async def check_category(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext, cat_id: int) -> None:
    url = f"{BASE_URL}/merchant/market/v2/products/{cat_id}"
    params = {"app-key": API_KEY}
    body = {"pageNum": 1, "pageSize": 5}
    try:
        async with session.post(url, params=params, json=body, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
            success = data.get("success")
            err_code = data.get("errorCode")
            d = data.get("data")
            if success and isinstance(d, dict):
                items = d.get("list") or d.get("records") or []
                if items:
                    first = items[0]
                    name = first.get("itemName") or first.get("name") or str(first)[:60]
                    item_id = first.get("id") or first.get("productId") or first.get("itemId") or "?"
                    price = first.get("price") or first.get("sellPrice") or "?"
                    print(f"[{cat_id}] OK {len(items)} items | first: '{name[:50]}' id={item_id} price={price}")
            elif not success:
                print(f"[{cat_id}] ERR {err_code}: {data.get('errorMsg', '')}")
    except Exception as e:
        print(f"[{cat_id}] EXCEPTION: {e}")


async def main() -> None:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession() as session:
        # Сканируем диапазон 1-300
        for cat_id in range(1, 301):
            await check_category(session, ssl_ctx, cat_id)
            if cat_id % 50 == 0:
                print(f"--- прошли {cat_id} ---")


asyncio.run(main())
