"""Test the found endpoints and print responses as ASCII."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import aiohttp

BASE_URL = "https://openapi.c5game.com"
APP_KEY = os.getenv("C5GAME_APP_KEY", "")

TESTS = [
    ("GET", "/merchant/account/v1/balance", None),
    ("GET", "/merchant/order/v1/list", None),
    ("GET", "/merchant/order/v1/detail", None),
    ("POST", "/merchant/order/v1/cancel", {}),
    # Try some more patterns with different naming
    ("GET", "/merchant/sell/v1/list", None),
    ("POST", "/merchant/sell/v1/list", {}),
    ("GET", "/merchant/csgo/v1/list", None),
    ("GET", "/merchant/dota2/v1/list", None),
    ("GET", "/merchant/game/v1/list", None),
    ("POST", "/merchant/game/v1/list", {}),
    ("GET", "/merchant/market/v1/list", None),
    ("GET", "/merchant/search/v1/list", None),
    ("POST", "/merchant/search/v1/list", {}),
    ("POST", "/merchant/search/v1/query", {}),
    # Sell/listing
    ("GET", "/merchant/selling/v1/list", None),
    ("POST", "/merchant/selling/v1/list", {}),
    ("GET", "/merchant/onsale/v1/list", None),
    ("POST", "/merchant/onsale/v1/list", {}),
    # Hash name
    ("GET", "/merchant/hashname/v1/list", None),
    ("POST", "/merchant/hashname/v1/list", {}),
    # Steam inventory
    ("GET", "/merchant/steam-inventory/v1/list", None),
    ("GET", "/merchant/backpack/v1/list", None),
]


async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(
        base_url=BASE_URL, timeout=timeout, connector=connector,
    ) as session:
        for method, path, body in TESTS:
            params = {"app-key": APP_KEY}
            try:
                async with session.request(
                    method, path, params=params, json=body,
                ) as resp:
                    status = resp.status
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {"raw": (await resp.text())[:200]}
            except Exception as e:
                status = -1
                data = {"error": str(e)[:100]}

            if status != 404:
                # Encode to ASCII for Windows console
                txt = json.dumps(data, ensure_ascii=True, indent=None)[:300]
                print(f"  {method:4} {path:45} -> {status}")
                print(f"       {txt}")
            else:
                print(f"  {method:4} {path:45} -> 404")

            await asyncio.sleep(0.15)


if __name__ == "__main__":
    asyncio.run(main())
