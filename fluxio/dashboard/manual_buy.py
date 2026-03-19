"""Ручная покупка из дашборда (полуавтоматический режим).

Два эндпоинта:
  GET  /api/v1/manual-buy/preflight  — данные для диалога подтверждения
  POST /api/v1/manual-buy            — выполнение реальной покупки
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from loguru import logger
from starlette.responses import JSONResponse

from fluxio.api.cs2dt_client import CS2DTClient
from fluxio.config import FeesConfig, config
from fluxio.core.safety import run_all_checks
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.services.container import ServiceContainer
from fluxio.utils.redis_client import (
    CHANNEL_PURCHASES,
    KEY_PURCHASED_IDS,
    get_redis,
)

router = APIRouter()

# Блокировка от параллельных покупок (одна покупка за раз)
_buy_lock = asyncio.Lock()


def _get_container(request: Request) -> ServiceContainer:
    """Получить ServiceContainer из app state."""
    return request.app.state.container


# ─── Preflight ───────────────────────────────────────────────────────────────


@router.get("/api/v1/manual-buy/preflight")
async def preflight(request: Request, market_hash_name: str = "") -> JSONResponse:
    """Подготовка к покупке: live цена, баланс, расчёт профита."""
    if not market_hash_name:
        return JSONResponse({"available": False, "error": "Не указано имя предмета"}, 400)

    container = _get_container(request)

    # Получаем CS2DT клиент
    if not container or not container.is_initialized(CS2DTClient):
        return JSONResponse({"available": False, "error": "CS2DT клиент не инициализирован"}, 503)

    cs2dt: CS2DTClient = await container.get(CS2DTClient)

    # Находим предмет в БД
    async with UnitOfWork(async_session_factory) as uow:
        item = await uow.items.get_by_name(market_hash_name)

    if not item:
        return JSONResponse({"available": False, "error": f"Предмет не найден в БД"}, 404)

    if not item.cs2dt_item_id:
        return JSONResponse({"available": False, "error": "Нет cs2dt_item_id для предмета"}, 404)

    # Получаем свежие лоты и баланс параллельно
    try:
        sell_task = cs2dt.get_sell_list(
            item_id=item.cs2dt_item_id,
            order_by=2,  # цена по возрастанию
            limit=5,
            max_price=str(config.trading.max_price_usd),
        )
        balance_task = cs2dt.get_balance()
        sell_data, balance_data = await asyncio.gather(sell_task, balance_task)
    except Exception as e:
        logger.error(f"ManualBuy preflight: ошибка API: {e}")
        return JSONResponse({"available": False, "error": f"Ошибка API: {e}"}, 502)

    listings = sell_data.get("list", []) if sell_data else []
    if not listings:
        return JSONResponse({"available": False, "error": "Нет доступных лотов на CS2DT"})

    cheapest = listings[0]
    cheapest_price = float(cheapest.get("price", 0))
    product_id = str(cheapest.get("id", ""))

    if cheapest_price <= 0 or not product_id:
        return JSONResponse({"available": False, "error": "Некорректные данные лота"})

    balance = float(balance_data.get("data", 0))
    steam_price = float(item.steam_price_usd or 0)
    net_steam = FeesConfig.calc_net_steam(
        steam_price, config.fees.steam_valve_fee_percent, config.fees.steam_game_fee_percent
    ) if steam_price else 0
    expected_profit = round(net_steam - cheapest_price, 4) if net_steam else None
    discount = round((net_steam - cheapest_price) / cheapest_price * 100, 1) if cheapest_price and net_steam else 0

    return JSONResponse({
        "available": True,
        "market_hash_name": market_hash_name,
        "product_id": product_id,
        "cheapest_price_usd": cheapest_price,
        "steam_price_usd": steam_price,
        "net_steam_usd": net_steam,
        "discount_percent": discount,
        "expected_profit_usd": expected_profit,
        "balance_usd": balance,
        "balance_after_usd": round(balance - cheapest_price, 2),
        "volume_24h": item.steam_volume_24h or 0,
        "listings_count": len(listings),
        "image_url": item.image_url or "",
    })


# ─── Buy ─────────────────────────────────────────────────────────────────────


@router.post("/api/v1/manual-buy")
async def manual_buy(request: Request) -> JSONResponse:
    """Выполнить реальную покупку предмета."""
    # Парсим тело запроса
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Некорректный JSON"}, 400)

    market_hash_name: str = body.get("market_hash_name", "").strip()
    max_price_usd: float = float(body.get("max_price_usd", 0))

    if not market_hash_name:
        return JSONResponse({"ok": False, "error": "Не указано имя предмета"}, 400)
    if max_price_usd <= 0:
        return JSONResponse({"ok": False, "error": "Некорректная максимальная цена"}, 400)

    # Проверяем TRADE_URL
    trade_url = config.env.trade_url
    if not trade_url:
        return JSONResponse(
            {"ok": False, "error": "TRADE_URL не задан в .env", "error_code": "no_trade_url"}, 500
        )

    container = _get_container(request)
    if not container or not container.is_initialized(CS2DTClient):
        return JSONResponse(
            {"ok": False, "error": "CS2DT клиент недоступен", "error_code": "cs2dt_unavailable"}, 503
        )

    # Блокировка — одна покупка за раз
    if _buy_lock.locked():
        return JSONResponse(
            {"ok": False, "error": "Другая покупка уже выполняется, подождите", "error_code": "busy"}, 429
        )

    async with _buy_lock:
        return await _execute_buy(container, market_hash_name, max_price_usd, trade_url)


async def _execute_buy(
    container: ServiceContainer,
    market_hash_name: str,
    max_price_usd: float,
    trade_url: str,
) -> JSONResponse:
    """Внутренняя логика покупки (под блокировкой)."""
    cs2dt: CS2DTClient = await container.get(CS2DTClient)

    # 1. Находим предмет в БД
    async with UnitOfWork(async_session_factory) as uow:
        item = await uow.items.get_by_name(market_hash_name)

    if not item:
        return JSONResponse(
            {"ok": False, "error": "Предмет не найден в БД", "error_code": "not_found"}, 404
        )
    if not item.cs2dt_item_id:
        return JSONResponse(
            {"ok": False, "error": "Нет cs2dt_item_id", "error_code": "no_cs2dt_id"}, 404
        )

    steam_price_usd = float(item.steam_price_usd or 0)
    net_steam = FeesConfig.calc_net_steam(
        steam_price_usd, config.fees.steam_valve_fee_percent, config.fees.steam_game_fee_percent
    ) if steam_price_usd else 0

    # 2. Получаем свежие лоты с CS2DT
    try:
        sell_data = await cs2dt.get_sell_list(
            item_id=item.cs2dt_item_id,
            order_by=2,
            limit=5,
            max_price=str(max_price_usd),
        )
    except Exception as e:
        logger.error(f"ManualBuy: ошибка get_sell_list [{market_hash_name}]: {e}")
        return JSONResponse(
            {"ok": False, "error": f"Ошибка получения лотов: {e}", "error_code": "api_error"}, 502
        )

    listings = sell_data.get("list", []) if sell_data else []
    if not listings:
        return JSONResponse(
            {"ok": False, "error": "Нет доступных лотов на CS2DT", "error_code": "no_listings"}
        )

    # 3. Выбираем самый дешёвый лот
    listing = listings[0]
    product_id = str(listing.get("id", ""))
    listing_price = float(listing.get("price", 0))

    if not product_id or listing_price <= 0:
        return JSONResponse(
            {"ok": False, "error": "Некорректные данные лота", "error_code": "no_listings"}
        )

    # 4. Stale price protection — цена не выросла выше подтверждённой
    if listing_price > max_price_usd:
        return JSONResponse({
            "ok": False,
            "error": f"Цена выросла: ${listing_price:.4f} > подтверждённая ${max_price_usd:.4f}",
            "error_code": "price_changed",
        })

    discount_percent = round(
        (net_steam - listing_price) / listing_price * 100, 2
    ) if listing_price and net_steam else 0

    # 5. Safety checks — все 5 проверок
    async with UnitOfWork(async_session_factory) as uow:
        safety = await run_all_checks(
            cs2dt_client=cs2dt,
            session=uow.session,
            product_id=product_id,
            market_hash_name=market_hash_name,
            price_usd=listing_price,
        )

        if not safety:
            return JSONResponse({
                "ok": False,
                "error": f"Проверка безопасности: {safety.reason}",
                "error_code": "safety_check_failed",
            })

        # 6. Создаём Purchase в статусе pending (до вызова API)
        out_trade_no = str(uuid.uuid4())

        purchase = await uow.purchases.create(
            product_id=product_id,
            market_hash_name=market_hash_name,
            price_usd=listing_price,
            steam_price_usd=steam_price_usd,
            discount_percent=discount_percent,
            status="pending",
            dry_run=False,
        )
        await uow.commit()

        # 7. Реальная покупка через API
        try:
            result = await cs2dt.buy(
                product_id=product_id,
                out_trade_no=out_trade_no,
                trade_url=trade_url,
                max_price=listing_price,
            )

            order_id = str(result.get("orderId", ""))
            buy_price = float(result.get("buyPrice", listing_price))
            delivery = result.get("delivery", 0)

            purchase.status = "success"
            purchase.order_id = order_id
            purchase.price_usd = buy_price
            purchase.api_response = result

            # Сохраняем active_order для трекинга доставки
            if order_id:
                await uow.purchases.save_active_order(
                    order_id=order_id,
                    purchase_id=purchase.id,
                    status="pending",
                )

            await uow.commit()

            logger.info(
                f"ManualBuy: КУПЛЕНО [{market_hash_name}] "
                f"цена=${buy_price:.4f}, ордер={order_id}, "
                f"доставка={'авто' if delivery == 2 else 'P2P'}"
            )

        except Exception as e:
            purchase.status = "failed"
            purchase.api_response = {"error": str(e)}
            await uow.commit()
            logger.error(f"ManualBuy: ошибка покупки [{market_hash_name}]: {e}")
            return JSONResponse({
                "ok": False,
                "error": f"Ошибка API покупки: {e}",
                "error_code": "api_error",
            }, 502)

    # 8. Redis: идемпотентность + pub/sub уведомление
    try:
        redis = await get_redis()
        await redis.sadd(KEY_PURCHASED_IDS, product_id)
        await redis.publish(CHANNEL_PURCHASES, json.dumps({
            "event": "purchase",
            "item": market_hash_name,
            "price_usd": buy_price,
            "discount_percent": discount_percent,
            "dry_run": False,
            "manual": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as e:
        logger.debug(f"ManualBuy: ошибка Redis после покупки: {e}")

    return JSONResponse({
        "ok": True,
        "order_id": order_id,
        "buy_price": buy_price,
        "product_id": product_id,
        "delivery": delivery,
        "market_hash_name": market_hash_name,
        "discount_percent": discount_percent,
    })
