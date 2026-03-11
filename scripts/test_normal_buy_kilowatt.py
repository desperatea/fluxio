"""Тест покупки Kilowatt Case через Normal Buy API (productId из URL)."""
import asyncio
import aiohttp
import ssl
import os
import uuid
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from dotenv import load_dotenv

load_dotenv("e:/c5games/.env")

API_KEY = os.getenv("C5GAME_APP_KEY", "")
TRADE_URL = os.getenv("TRADE_URL", "")
BASE_URL = "https://openapi.c5game.com"


async def main() -> None:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    product_id = "1229154709812346880"
    out_trade_no = f"arb_{uuid.uuid4().hex[:16]}"
    buy_price = 25.0  # текущая цена Kilowatt Case примерно

    body = {
        "productId": product_id,
        "buyPrice": buy_price,
        "outTradeNo": out_trade_no,
        "tradeUrl": TRADE_URL,
    }
    print(f"Normal Buy body: {body}")
    print()

    async with aiohttp.ClientSession() as session:
        url = f"{BASE_URL}/merchant/trade/v2/normal-buy"
        params = {"app-key": API_KEY}
        async with session.post(url, params=params, json=body, ssl=ssl_ctx) as resp:
            data = await resp.json(content_type=None)
            print(f"HTTP Status: {resp.status}")
            print(f"Response: {data}")


asyncio.run(main())
