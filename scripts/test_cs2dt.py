"""Тест подключения с ключами CS2DT — баланс + листинг рынка."""

import asyncio
import json
import os
import ssl
import sys

# Фикс кодировки консоли Windows
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://openapi.c5game.com"


async def test_api(app_key: str, label: str) -> None:
    """Проверить баланс и листинг рынка для указанного ключа."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        base_url=BASE_URL, timeout=timeout, connector=connector
    ) as session:
        masked = f"{app_key[:6]}***{app_key[-4:]}"
        print(f"\n{'='*60}")
        print(f"[{label}] Ключ: {masked}")
        print(f"{'='*60}")

        # 1. Баланс
        print("\n--- Баланс ---")
        try:
            async with session.get(
                "/merchant/account/v1/balance",
                params={"app-key": app_key},
            ) as resp:
                raw = await resp.text()
                data = json.loads(raw)
                print(f"  HTTP {resp.status}")
                print(f"  success: {data.get('success')}")
                if data.get("success"):
                    print(f"  data: {json.dumps(data.get('data'), indent=2, ensure_ascii=False)}")
                else:
                    print(f"  errorCode: {data.get('errorCode')}")
                    print(f"  errorMsg: {data.get('errorMsg')}")
        except Exception as e:
            print(f"  ОШИБКА: {e}")

        await asyncio.sleep(1)

        # 2. Рыночный листинг Dota 2 (gameId=570)
        print("\n--- Рынок Dota 2 (gameId=570) ---")
        try:
            async with session.post(
                "/merchant/market/v2/products/570",
                params={"app-key": app_key},
                json={"pageNum": 1, "pageSize": 5},
            ) as resp:
                raw = await resp.text()
                if resp.status == 429 or "Too Many" in raw[:50]:
                    print(f"  HTTP {resp.status} — Rate limit! Ждём 3с...")
                    await asyncio.sleep(3)
                    # Повторная попытка
                    async with session.post(
                        "/merchant/market/v2/products/570",
                        params={"app-key": app_key},
                        json={"pageNum": 1, "pageSize": 5},
                    ) as resp2:
                        raw = await resp2.text()
                data = json.loads(raw)
                print(f"  HTTP {resp.status}")
                print(f"  success: {data.get('success')}")
                if data.get("success"):
                    d = data.get("data", {})
                    items = d.get("list", [])
                    print(f"  total: {d.get('total')}, pages: {d.get('pages')}")
                    for item in items[:3]:
                        print(f"    - {item.get('itemName', '?')} | "
                              f"цена: {item.get('price')} CNY | "
                              f"id: {item.get('id')}")
                else:
                    print(f"  errorCode: {data.get('errorCode')}")
                    print(f"  errorMsg: {data.get('errorMsg')}")
        except Exception as e:
            print(f"  ОШИБКА: {e}")

        await asyncio.sleep(2)

        # 3. Рыночный листинг CS2 (gameId=730)
        print("\n--- Рынок CS2 (gameId=730) ---")
        try:
            async with session.post(
                "/merchant/market/v2/products/730",
                params={"app-key": app_key},
                json={"pageNum": 1, "pageSize": 5},
            ) as resp:
                raw = await resp.text()
                if resp.status == 429 or "Too Many" in raw[:50]:
                    print(f"  HTTP {resp.status} — Rate limit! Ждём 3с...")
                    await asyncio.sleep(3)
                    async with session.post(
                        "/merchant/market/v2/products/730",
                        params={"app-key": app_key},
                        json={"pageNum": 1, "pageSize": 5},
                    ) as resp2:
                        raw = await resp2.text()
                data = json.loads(raw)
                print(f"  HTTP {resp.status}")
                print(f"  success: {data.get('success')}")
                if data.get("success"):
                    d = data.get("data", {})
                    items = d.get("list", [])
                    print(f"  total: {d.get('total')}, pages: {d.get('pages')}")
                    for item in items[:3]:
                        print(f"    - {item.get('itemName', '?')} | "
                              f"цена: {item.get('price')} CNY | "
                              f"id: {item.get('id')}")
                else:
                    print(f"  errorCode: {data.get('errorCode')}")
                    print(f"  errorMsg: {data.get('errorMsg')}")
        except Exception as e:
            print(f"  ОШИБКА: {e}")

        await asyncio.sleep(1)

        # 4. Заказы покупателя
        print("\n--- Заказы покупателя ---")
        try:
            async with session.post(
                "/merchant/order/v2/buyer",
                params={"app-key": app_key},
                json={"page": 1, "limit": 5},
            ) as resp:
                raw = await resp.text()
                data = json.loads(raw)
                print(f"  HTTP {resp.status}")
                print(f"  success: {data.get('success')}")
                if data.get("success"):
                    d = data.get("data", {})
                    orders = d.get("list", [])
                    print(f"  total: {d.get('total')}")
                    for o in orders[:3]:
                        print(f"    - orderId: {o.get('orderId')} | "
                              f"status: {o.get('status')} | "
                              f"price: {o.get('actualPay')}")
                else:
                    print(f"  errorCode: {data.get('errorCode')}")
                    print(f"  errorMsg: {data.get('errorMsg')}")
        except Exception as e:
            print(f"  ОШИБКА: {e}")


async def main() -> None:
    c5_key = os.getenv("C5GAME_APP_KEY", "")
    cs2dt_key = os.getenv("CS2DT_APP_KEY", "")

    if c5_key:
        await test_api(c5_key, "C5GAME (старый)")

    if cs2dt_key:
        await test_api(cs2dt_key, "CS2DT (новый)")

    if not c5_key and not cs2dt_key:
        print("Ни один ключ не найден в .env!")


if __name__ == "__main__":
    asyncio.run(main())
