"""Скрипт проверки подключения к C5Game API.

Запуск: python scripts/test_api.py

Проверяет:
1. Баланс аккаунта (подтверждённый эндпоинт)
2. Steam-информация аккаунта
3. Список товаров на рынке (верификация эндпоинта)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from fluxio.api.c5game_client import C5GameClient, C5GameAPIError, ENDPOINTS
from fluxio.utils.logger import setup_logging

setup_logging("DEBUG")


async def test_endpoint(
    client: C5GameClient,
    name: str,
    coro,
) -> dict | None:
    """Протестировать один эндпоинт и вывести результат."""
    print(f"\n{'='*60}")
    print(f"  Тест: {name}")
    print(f"{'='*60}")

    start = time.monotonic()
    try:
        result = await coro
        elapsed = (time.monotonic() - start) * 1000
        print(f"  Статус: УСПЕХ ({elapsed:.0f}мс)")
        print(f"  Ответ: {result}")
        return result
    except C5GameAPIError as e:
        elapsed = (time.monotonic() - start) * 1000
        print(f"  Статус: ОШИБКА API ({elapsed:.0f}мс)")
        print(f"  Код: {e.status_code}, Ошибка: {e}")
        if e.response_data:
            print(f"  Полный ответ: {e.response_data}")
        return None
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        print(f"  Статус: ИСКЛЮЧЕНИЕ ({elapsed:.0f}мс)")
        print(f"  Тип: {type(e).__name__}: {e}")
        return None


async def main() -> None:
    """Запуск всех проверок."""
    app_key = os.getenv("C5GAME_APP_KEY", "")
    app_secret = os.getenv("C5GAME_APP_SECRET", "")
    if not app_key or app_key == "your_app_key_here":
        print("ОШИБКА: C5GAME_APP_KEY не задан в .env")
        print("Создай .env из .env.example и впиши свои ключи")
        sys.exit(1)

    print(f"APP_KEY:    {app_key[:4]}***{app_key[-4:]}")
    print(f"APP_SECRET: {'задан' if app_secret else 'НЕ задан'}")
    print(f"\nЭндпоинты для проверки:")
    for key, path in ENDPOINTS.items():
        print(f"  {key}: {path}")

    client = C5GameClient()

    try:
        # 1. Баланс (подтверждённый эндпоинт)
        balance = await test_endpoint(
            client,
            "Баланс аккаунта (GET /merchant/account/v1/balance)",
            client.get_balance(),
        )

        # 2. Steam информация
        steam = await test_endpoint(
            client,
            "Steam информация (GET /merchant/account/v1/steam)",
            client.get_steam_info(),
        )

        # 3. Список товаров на рынке (Dota 2, 1 страница, 5 штук)
        products = await test_endpoint(
            client,
            "Список товаров Dota 2 (POST /merchant/product/v1/list)",
            client.get_product_list(app_id=570, page_num=1, page_size=5),
        )

        # 4. Если товары получены — попробуем статистику
        if products and isinstance(products, dict):
            # Пытаемся извлечь hashNames из ответа
            items = products.get("list", products.get("items", products.get("records", [])))
            if items and isinstance(items, list) and len(items) > 0:
                # Пробуем разные варианты ключа для hashName
                first_item = items[0]
                hash_name = (
                    first_item.get("hashName")
                    or first_item.get("market_hash_name")
                    or first_item.get("name")
                )
                if hash_name:
                    print(f"\n  Найден предмет: {hash_name}")
                    await test_endpoint(
                        client,
                        f"Статистика по hashName: {hash_name}",
                        client.get_item_stats_batch([hash_name]),
                    )

        # Итого
        print(f"\n{'='*60}")
        print("  ИТОГО:")
        print(f"  Баланс: {'OK' if balance else 'ОШИБКА'}")
        print(f"  Steam: {'OK' if steam else 'ОШИБКА'}")
        print(f"  Товары: {'OK' if products else 'ОШИБКА'}")
        print(f"{'='*60}")

        if not balance and not steam and not products:
            print("\nВсе эндпоинты вернули ошибку.")
            print("Возможные причины:")
            print("  1. Неверный API-ключ")
            print("  2. Пути эндпоинтов отличаются от документации")
            print("  3. IP не в белом списке")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
