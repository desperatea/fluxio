"""HTTP-клиент для CS2DT (HaloSkins) Open API.

Base URL: https://openapi.cs2dt.com
Авторизация: query параметр app-key
IP Whitelist: обязательно (управление через https://admin.cs2dt.com/#/user/api)
Подпись: не требуется
Цены: в USD

Документация: https://opendoc.cs2dt.com/en
Эндпоинты верифицированы 2026-03-08.
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

import aiohttp
from loguru import logger

from fluxio.config import config
from fluxio.utils.rate_limiter import (
    RETRY_429,
    RETRY_5XX,
    RetryConfig,
    TokenBucketRateLimiter,
)

BASE_URL = "https://openapi.cs2dt.com"

# ──────────────────────────────────────────────────────────────────────────────
# Эндпоинты v2 — верифицированы 2026-03-08
# ──────────────────────────────────────────────────────────────────────────────
ENDPOINTS = {
    # Аккаунт
    "balance":           "GET  /v2/user/v1/t-coin/balance",
    "steam_check":       "POST /v2/user/steam-check/create",
    "steam_info":        "GET  /v2/user/steam-info",
    "usd_cny_rate":      "GET  /v1/currency/rate/usd-cny",

    # Рынок — поиск и листинги
    "market_search":     "GET  /v2/product/v2/search",
    "sell_list":         "GET  /v2/product/v1/sell/list",
    "sell_product":      "GET  /v2/product/v1/sell/product",
    "product_info":      "POST /v2/product/v1/info",
    "product_ids":       "POST /v2/product/v2/list/ids",
    "price_info":        "POST /v2/product/price/info",
    "filters":           "GET  /v2/product/v1/filters",

    # Покупка
    "trade_buy":         "POST /v2/trade/v2/buy",
    "trade_quick_buy":   "POST /v2/trade/v2/quick-buy",

    # Заказы покупателя
    "order_buyer_list":  "GET  /v2/order/buyer/list",
    "order_buyer_v2":    "POST /v2/order/buyer/v2/list",
    "order_detail":      "GET  /v1/order/v2/buy/detail",
    "order_cancel":      "POST /v1/order/buyer-cancel",

    # Продажа
    "sell_create":       "POST /v2/open/sell/v1/create",
    "sell_cancel":       "POST /v2/open/sell/v1/cancel",
    "sell_update":       "POST /v2/open/sell/v1/update",
    "sell_my_list":      "GET  /v2/open/sell/v1/list",
    "sell_price_ref":    "POST /v2/open/sell/v1/price/reference",
}

_PATHS: dict[str, tuple[str, str]] = {}
for _k, _v in ENDPOINTS.items():
    _method, _path = _v.split(None, 1)
    _PATHS[_k] = (_method.strip(), _path.strip())


class CS2DTClient:
    """Асинхронный HTTP-клиент для CS2DT (HaloSkins) Open API.

    Возможности:
    - Token Bucket rate limiting
    - Semaphore для ограничения параллельных запросов
    - Exponential backoff для 429 и 5xx ошибок
    - Логирование запросов и ошибок
    """

    def __init__(self) -> None:
        self._app_key: str = config.env.cs2dt_app_key
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = TokenBucketRateLimiter(
            rate=5.0, burst=5, name="cs2dt"
        )
        self._semaphore = asyncio.Semaphore(3)

    @property
    def _masked_key(self) -> str:
        """Замаскированный APP_KEY для логов."""
        if len(self._app_key) <= 8:
            return "***"
        return f"{self._app_key[:4]}***{self._app_key[-4:]}"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать aiohttp-сессию."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector()
            self._session = aiohttp.ClientSession(
                base_url=BASE_URL,
                timeout=timeout,
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        """Закрыть HTTP-сессию."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("CS2DT HTTP-сессия закрыта")

    async def _request(
        self,
        endpoint_key: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> dict[str, Any]:
        """Выполнить HTTP-запрос к CS2DT API.

        Args:
            endpoint_key: ключ из ENDPOINTS.
            params: дополнительные query-параметры.
            json_body: тело запроса (для POST).
            retry_config: конфигурация повторных попыток.

        Returns:
            Поле ``data`` из JSON-ответа.

        Raises:
            CS2DTAPIError: при бизнес-ошибке или HTTP-ошибке.
        """
        method, path = _PATHS[endpoint_key]
        return await self._raw_request(
            method, path, params=params, json_body=json_body,
            retry_config=retry_config,
        )

    async def _raw_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> dict[str, Any]:
        """Выполнить запрос по произвольному пути.

        Поддерживает retry при 429 и 5xx ошибках.
        """
        query_params: dict[str, Any] = {"app-key": self._app_key}
        if params:
            query_params.update(params)
        session = await self._get_session()
        retries = retry_config or RETRY_5XX

        for attempt in range(retries.max_retries + 1):
            retry_delay: float | None = None

            async with self._semaphore:
                await self._rate_limiter.acquire()
                try:
                    logger.debug(
                        f"CS2DT → {method} {path} "
                        f"(key={self._masked_key}, попытка {attempt + 1})"
                    )

                    async with session.request(
                        method, path, params=query_params, json=json_body,
                        proxy=config.env.http_proxy or None,
                    ) as resp:
                        raw_bytes = await resp.read()
                        raw_text = raw_bytes.decode("utf-8")

                        # Rate limit
                        if resp.status == 429 or (
                            resp.status == 200 and "Too Many" in raw_text[:50]
                        ):
                            retry_delay = RETRY_429.get_delay(attempt)
                            logger.warning(
                                f"CS2DT rate limit {path} — "
                                f"ожидание {retry_delay:.1f}с (попытка {attempt + 1})"
                            )
                            if attempt >= RETRY_429.max_retries:
                                raise CS2DTAPIError(
                                    f"Rate limit после {attempt + 1} попыток",
                                    status_code=429,
                                )

                        if retry_delay is None:
                            if not raw_text or not raw_text.strip():
                                raise CS2DTAPIError(
                                    f"Пустой ответ от API ({path})",
                                    status_code=resp.status,
                                )
                            try:
                                raw = _json.loads(raw_text)
                            except _json.JSONDecodeError as e:
                                raise CS2DTAPIError(
                                    f"Невалидный JSON от API ({path}): {str(e)[:80]}",
                                    status_code=resp.status,
                                ) from e
                            if not isinstance(raw, dict):
                                raise CS2DTAPIError(
                                    f"Ответ не dict: {str(raw)[:100]}",
                                    status_code=resp.status,
                                )

                            logger.debug(
                                f"CS2DT ← {resp.status} {path} "
                                f"(success={raw.get('success')})"
                            )

                            if resp.status >= 500:
                                retry_delay = retries.get_delay(attempt)
                                logger.warning(
                                    f"CS2DT {resp.status} {path} — "
                                    f"retry через {retry_delay:.1f}с (попытка {attempt + 1})"
                                )
                                if attempt >= retries.max_retries:
                                    raise CS2DTAPIError(
                                        f"Серверная ошибка {resp.status}",
                                        status_code=resp.status,
                                        response_data=raw,
                                    )

                            if retry_delay is None:
                                if not raw.get("success", False):
                                    raise CS2DTAPIError(
                                        f"API [{raw.get('errorCode')}]: {raw.get('errorMsg')}",
                                        status_code=resp.status,
                                        error_code=raw.get("errorCode"),
                                        response_data=raw,
                                    )
                                return raw.get("data") if raw.get("data") is not None else raw

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    retry_delay = retries.get_delay(attempt)
                    logger.error(
                        f"CS2DT сетевая ошибка {path}: {e} — "
                        f"retry через {retry_delay:.1f}с (попытка {attempt + 1})"
                    )
                    if attempt >= retries.max_retries:
                        raise

            # Retry sleep вне semaphore
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)

        raise CS2DTAPIError("Исчерпаны попытки запроса")

    # ──────────────────────────────────────────────────────
    # Аккаунт
    # ──────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """Баланс аккаунта (T-Coin).

        Returns:
            {"userId": int, "name": str, "data": str}
            Поле ``data`` содержит сумму баланса в USD строкой.
        """
        return await self._request("balance")

    async def get_usd_cny_rate(self) -> float:
        """Текущий курс USD/CNY.

        Returns:
            Курс как float (например 6.9187).
        """
        result = await self._request("usd_cny_rate")
        return float(result) if not isinstance(result, dict) else float(result.get("data", 0))

    async def check_steam_account(
        self,
        trade_url: str,
        app_id: int = 730,
        check_type: int = 1,
    ) -> dict[str, Any]:
        """Проверить статус Steam аккаунта.

        Args:
            trade_url: Steam Trade URL.
            app_id: ID игры (730=CS2, 570=Dota2).
            check_type: 1=для покупки, 2=для продажи.

        Returns:
            {"checkStatus": int, "steamInfo": {...}, "statusList": [...]}
            checkStatus: 0=проверка, 1=ок, 2=проблема.
        """
        return await self._request(
            "steam_info",
            params={"type": check_type, "appId": app_id, "tradeUrl": trade_url},
        )

    # ──────────────────────────────────────────────────────
    # Рынок — поиск предметов
    # ──────────────────────────────────────────────────────

    async def search_market(
        self,
        app_id: int = 570,
        page: int = 1,
        limit: int = 50,
        order_by: int = 0,
        keyword: str | None = None,
        min_price: str | None = None,
        max_price: str | None = None,
        only_auto_deliver: bool = False,
    ) -> dict[str, Any]:
        """Поиск предметов на рынке.

        Args:
            app_id: ID игры (570=Dota2, 730=CS2).
            page: номер страницы.
            limit: кол-во на странице.
            order_by: сортировка (0=по времени, 1=цена↑, 2=цена↓).
            keyword: поиск по ключевому слову.
            min_price: мин. цена (USD).
            max_price: макс. цена (USD).
            only_auto_deliver: только с автодоставкой.

        Returns:
            {"total": int, "pages": int, "page": int, "limit": int,
             "list": [{itemId, itemName, marketHashName, priceInfo, ...}]}
        """
        params: dict[str, Any] = {
            "appId": app_id,
            "page": page,
            "limit": limit,
            "orderBy": order_by,
        }
        if keyword:
            params["keyword"] = keyword
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price
        if only_auto_deliver:
            params["onlyWithAutoDeliverPrice"] = 1
        return await self._request("market_search", params=params)

    async def get_sell_list(
        self,
        item_id: int | str,
        page: int = 1,
        limit: int = 50,
        order_by: int = 2,
        delivery: int | None = None,
        min_price: str | None = None,
        max_price: str | None = None,
    ) -> dict[str, Any]:
        """Листинги конкретного предмета (лоты на продажу).

        Args:
            item_id: ID предмета (из search_market).
            page: номер страницы.
            limit: кол-во на странице.
            order_by: сортировка (0=по умолч, 2=цена↑, 3=цена↓).
            delivery: 1=ручная, 2=автоматическая.
            min_price: мин. цена.
            max_price: макс. цена.

        Returns:
            {"total": int, "list": [{id, price, cnyPrice, delivery, assetInfo, ...}]}
            Поле ``id`` = productId для покупки.
        """
        params: dict[str, Any] = {
            "itemId": item_id,
            "page": page,
            "limit": limit,
            "orderBy": order_by,
        }
        if delivery is not None:
            params["delivery"] = delivery
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price
        return await self._request("sell_list", params=params)

    async def get_sell_product(self, product_id: str | int) -> dict[str, Any]:
        """Детали конкретного лота.

        Args:
            product_id: ID лота (из get_sell_list).
        """
        return await self._request("sell_product", params={"productId": str(product_id)})

    async def get_prices_batch(
        self,
        hash_names: list[str],
        app_id: int = 570,
    ) -> list[dict[str, Any]]:
        """Пакетный запрос цен по marketHashName (макс 200).

        Args:
            hash_names: список Steam Market Hash Name.
            app_id: ID игры.

        Returns:
            [{marketHashName, price, quantity, autoDeliverPrice,
              avgPrice, medianPrice, ...}]
        """
        return await self._request(
            "price_info",
            json_body={"appId": app_id, "marketHashNameList": hash_names},
        )

    async def get_product_info(
        self,
        product_ids: list[int],
    ) -> dict[str, Any]:
        """Инфо по списку productId (макс 5000).

        Args:
            product_ids: список ID лотов.

        Returns:
            {"totalCount": int, "productInfoDTOList": [{id, price, ...}]}
        """
        return await self._request(
            "product_info",
            json_body={"productIdList": product_ids},
        )

    async def get_products_by_ids(
        self,
        product_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Статус лотов по productId (макс 100).

        Возвращает только лоты которые ещё в продаже.
        """
        return await self._request(
            "product_ids",
            json_body={"productIds": product_ids},
        )

    async def get_filters(self, app_id: int = 570) -> dict[str, Any]:
        """Фильтры для поиска (категории, типы, редкости и т.д.)."""
        return await self._request("filters", params={"appId": app_id})

    # ──────────────────────────────────────────────────────
    # Покупка
    # ──────────────────────────────────────────────────────

    async def buy(
        self,
        product_id: int | str,
        out_trade_no: str,
        trade_url: str,
        max_price: float | None = None,
        buy_price: float | None = None,
    ) -> dict[str, Any]:
        """Покупка по productId (из sell_list).

        ⚠️ Реальная покупка!

        Args:
            product_id: ID лота.
            out_trade_no: уникальный ID заказа (UUID).
            trade_url: Steam Trade URL покупателя.
            max_price: макс. цена (если передан, buy_price игнорируется).
            buy_price: точная цена для проверки.

        Returns:
            {"buyPrice": float, "orderId": str, "delivery": int, "offerId": str|None}
        """
        body: dict[str, Any] = {
            "outTradeNo": out_trade_no,
            "tradeUrl": trade_url,
            "productId": int(product_id),
        }
        if max_price is not None:
            body["maxPrice"] = max_price
        elif buy_price is not None:
            body["buyPrice"] = buy_price
        return await self._request("trade_buy", json_body=body)

    async def quick_buy(
        self,
        out_trade_no: str,
        trade_url: str,
        max_price: float,
        item_id: int | str | None = None,
        app_id: int | None = None,
        market_hash_name: str | None = None,
        delivery: int | None = None,
        low_price: bool = False,
    ) -> dict[str, Any]:
        """Быстрая покупка (по itemId или marketHashName).

        ⚠️ Реальная покупка!

        Args:
            out_trade_no: уникальный ID заказа (UUID).
            trade_url: Steam Trade URL покупателя.
            max_price: макс. цена в USD.
            item_id: ID предмета (альтернатива marketHashName).
            app_id: ID игры (нужен с marketHashName).
            market_hash_name: Steam Market Hash Name.
            delivery: 1=ручная, 2=автоматическая.
            low_price: купить по минимальной цене.

        Returns:
            {"buyPrice": float, "orderId": str, "delivery": int, "offerId": str|None}
        """
        body: dict[str, Any] = {
            "outTradeNo": out_trade_no,
            "tradeUrl": trade_url,
            "maxPrice": max_price,
        }
        if item_id is not None:
            body["itemId"] = int(item_id)
        if app_id is not None:
            body["appId"] = app_id
        if market_hash_name is not None:
            body["marketHashName"] = market_hash_name
        if delivery is not None:
            body["delivery"] = delivery
        if low_price:
            body["lowPrice"] = 1
        return await self._request("trade_quick_buy", json_body=body)

    # ──────────────────────────────────────────────────────
    # Заказы покупателя
    # ──────────────────────────────────────────────────────

    async def get_buyer_orders(
        self,
        app_id: int = 570,
        page: int = 1,
        limit: int = 50,
        status: int | None = None,
    ) -> dict[str, Any]:
        """Список заказов покупателя.

        Args:
            app_id: ID игры.
            page: номер страницы.
            limit: кол-во на странице.
            status: фильтр (1=WaitingDelivery, 2=Delivery,
                    3=WaitingAccept, 10=Successful, 11=Fail).
        """
        params: dict[str, Any] = {
            "appId": app_id,
            "page": page,
            "limit": limit,
        }
        if status is not None:
            params["status"] = status
        return await self._request("order_buyer_list", params=params)

    async def get_buyer_orders_v2(
        self,
        app_id: int = 570,
        page: int = 1,
        limit: int = 50,
        status: int | None = None,
        order_ids: list[str] | None = None,
        out_trade_nos: list[str] | None = None,
    ) -> dict[str, Any]:
        """Список заказов покупателя v2 (POST, доп. фильтры).

        Args:
            app_id: ID игры.
            page: номер страницы.
            limit: кол-во на странице.
            status: фильтр по статусу.
            order_ids: фильтр по ID заказов.
            out_trade_nos: фильтр по внешним номерам.
        """
        params: dict[str, Any] = {
            "appId": app_id,
            "page": page,
            "limit": limit,
        }
        if status is not None:
            params["status"] = status
        if order_ids:
            params["orderIdList"] = order_ids
        if out_trade_nos:
            params["outTradeNos"] = out_trade_nos
        return await self._request("order_buyer_v2", params=params)

    async def get_order_detail(
        self,
        order_id: str | None = None,
        out_trade_no: str | None = None,
    ) -> dict[str, Any]:
        """Детали заказа покупателя.

        Args:
            order_id: ID заказа (приоритет).
            out_trade_no: внешний номер заказа.
        """
        params: dict[str, Any] = {}
        if order_id:
            params["orderId"] = order_id
        if out_trade_no:
            params["outTradeNo"] = out_trade_no
        return await self._request("order_detail", params=params)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Отмена заказа покупателя.

        ⚠️ Штраф 2% от цены (макс 10T, мин 0.01T).
        """
        return await self._request(
            "order_cancel",
            json_body={"orderId": order_id},
        )

    # ──────────────────────────────────────────────────────
    # Продажа
    # ──────────────────────────────────────────────────────

    async def create_sell(
        self,
        app_id: int,
        products: list[dict[str, Any]],
        asset_type: int = 1,
    ) -> bool:
        """Выставить предметы на продажу.

        Args:
            app_id: ID игры.
            products: список [{assetId, classId, instanceId, marketHashName, price, steamId}].
            asset_type: 1=обычный, 2=с cooldown.

        Returns:
            True при успехе.
        """
        result = await self._request(
            "sell_create",
            json_body={
                "appId": app_id,
                "assetType": asset_type,
                "products": products,
            },
        )
        return bool(result)

    async def cancel_sell(
        self,
        products: list[dict[str, str]],
        asset_type: int = 1,
    ) -> bool:
        """Снять предметы с продажи.

        Args:
            products: список [{"productId": "..."}].
            asset_type: 1=обычный, 2=с cooldown.
        """
        result = await self._request(
            "sell_cancel",
            json_body={"assetType": asset_type, "products": products},
        )
        return bool(result)

    async def update_sell_price(
        self,
        app_id: int,
        products: list[dict[str, Any]],
        request_id: str,
        asset_type: int = 1,
    ) -> bool:
        """Обновить цену лотов на продажу.

        Args:
            app_id: ID игры.
            products: список [{"productId": "...", "price": 1.5}].
            request_id: уникальный ID запроса.
            asset_type: 1=обычный, 2=с cooldown.
        """
        result = await self._request(
            "sell_update",
            json_body={
                "appId": app_id,
                "assetType": asset_type,
                "products": products,
                "requestId": request_id,
            },
        )
        return bool(result)

    async def get_my_sell_list(
        self,
        asset_type: int | None = None,
    ) -> dict[str, Any]:
        """Список моих лотов на продажу."""
        params: dict[str, Any] = {}
        if asset_type is not None:
            params["assetType"] = asset_type
        return await self._request("sell_my_list", params=params)

    async def get_sell_detail(self, product_id: str) -> dict[str, Any]:
        """Детали моего лота на продажу."""
        return await self._raw_request(
            "GET", f"/v2/open/sell/v1/detail/{product_id}",
        )


class CS2DTAPIError(Exception):
    """Ошибка API CS2DT."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: int | None = None,
        response_data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.response_data = response_data
