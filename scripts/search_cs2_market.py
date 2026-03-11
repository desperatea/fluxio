"""Ищем CS2 кейсы на маркете по категориям."""
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


async def search_category(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext, cat_id: int) -> None:
    url = f"{BASE_URL}/merchant/market/v2/products/{cat_id}"
    params = {"app-key": API_KEY}
    body = {"pageNum": 1, "pageSize": 10}
    async with session.post(url, params=params, json=body, ssl=ssl_ctx) as resp:
        data = await resp.json(content_type=None)
        if not data.get("success"):
            return
        d = data.get("data", {})
        if not isinstance(d, dict):
            return
        items = d.get("list") or d.get("records") or []
        if not items:
            return
        # Ищем кейсы
        for item in items:
            name = (item.get("itemName") or item.get("name") or "").lower()
            if "case" in name or "кейс" in name:
                print(f"[cat={cat_id}] CASE FOUND: {item}")
                return
        # Показываем первый предмет для ориентира
        first = items[0]
        name = first.get("itemName") or first.get("name") or str(first)[:80]
        print(f"[cat={cat_id}] {len(items)} items, first: {name[:60]}")


async def main() -> None:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession() as session:
        # Сканируем широкий диапазон
        for cat_id in range(100, 201):
            await search_category(session, ssl_ctx, cat_id)


asyncio.run(main())
