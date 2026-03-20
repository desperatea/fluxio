"""HTTP-клиент для C5Game Open API.

Base URL: https://openapi.c5game.com
Авторизация: query параметр app-key
Rate limit: 50 QPS (по документации)

Эндпоинты верифицированы 2026-03-05 путём прямых запросов.
Источник: ApiFox документация https://90nj2dplv3.apifox.cn/
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

import ssl

import aiohttp
from loguru import logger

from fluxio.config import config
from fluxio.utils.rate_limiter import (
    RETRY_429,
    RETRY_5XX,
    RetryConfig,
    TokenBucketRateLimiter,
)

# Базовый URL из документации
BASE_URL = "https://openapi.c5game.com"

# ──────────────────────────────────────────────────────────────────────────────
# Эндпоинты — верифицированы 2026-03-05 (тест вернул не-404)
# Источник: ApiFox https://90nj2dplv3.apifox.cn/
# ──────────────────────────────────────────────────────────────────────────────
ENDPOINTS = {
    # Аккаунт
    "balance":        "GET  /merchant/account/v1/balance",   # ✓ ok=True

    # Рыночный листинг: путь /merchant/market/v2/products/{gameId}
    # gameId передаётся через path, поэтому метод _request не используется напрямую
    # — см. метод search_market() ниже

    # Наши активные лоты на продажу
    "sale_search":    "GET  /merchant/sale/v1/search",       # ✓ (нужны параметры)
    "sale_create":    "POST /merchant/sale/v1/create",       # ✓ (нужны параметры)
    "sale_modify":    "POST /merchant/sale/v1/modify",       # ✓ (нужны параметры)
    "sale_cancel":    "POST /merchant/sale/v1/cancel",       # ✓ (нужны параметры)

    # Цены по hashName (пакетный запрос)
    "price_batch":    "POST /merchant/product/price/batch",  # ✓ (нужны параметры)

    # Заказы
    "order_buyer":    "POST /merchant/order/v2/buyer",       # ✓ ok=True
    "order_seller":   "POST /merchant/order/v2/seller",      # ✓ ok=True
    "order_detail":   "GET  /merchant/order/v2/buy/detail",  # ✓ (нужен orderId)

    # Покупка (требует активации 购买权限 от поддержки C5Game)
    "trade_quick":    "POST /merchant/trade/v2/quick-buy",   # ✓ структура верна
    "trade_normal":   "POST /merchant/trade/v2/normal-buy",  # ✓ структура верна
}

# Преобразованные пути для _request()
_PATHS: dict[str, tuple[str, str]] = {}
for _k, _v in ENDPOINTS.items():
    _method, _path = _v.split(None, 1)
    _PATHS[_k] = (_method.strip(), _path.strip())


class C5GameClient:
    """Асинхронный HTTP-клиент для C5Game Open API.

    Возможности:
    - Token Bucket rate limiting (50 QPS)
    - Semaphore для ограничения параллельных запросов (макс. 3)
    - Exponential backoff для 429 и 5xx ошибок
    - Логирование всех запросов (DEBUG) и ошибок (ERROR)
    - Маскировка API-ключа в логах
    """

    def __init__(self) -> None:
        self._app_key: str = config.env.c5game_app_key
        self._session: aiohttp.ClientSession | None = None
        # Реальный лимит C5Game ~1 QPS (несмотря на заявленные 50)
        self._rate_limiter = TokenBucketRateLimiter(
            rate=1.5, burst=1, name="c5game"
        )
        self._semaphore = asyncio.Semaphore(1)

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
            # C5Game использует самоподписанный сертификат
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
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
            logger.debug("C5Game HTTP-сессия закрыта")

    async def _request(
        self,
        endpoint_key: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> dict[str, Any]:
        """Выполнить HTTP-запрос к C5Game API.

        Args:
            endpoint_key: ключ из ENDPOINTS.
            params: дополнительные query-параметры.
            json_body: тело запроса (для POST).
            retry_config: конфигурация повторных попыток.

        Returns:
            Поле ``data`` из JSON-ответа (или полный ответ если data отсутствует).

        Raises:
            C5GameAPIError: при бизнес-ошибке или HTTP-ошибке API.
        """
        method, path = _PATHS[endpoint_key]
        query_params: dict[str, Any] = {"app-key": self._app_key}
        if params:
            query_params.update(params)

        session = await self._get_session()
        retries = retry_config or RETRY_5XX
        last_error: Exception | None = None

        for attempt in range(retries.max_retries + 1):
            async with self._semaphore:
                await self._rate_limiter.acquire()

                try:
                    logger.debug(
                        f"C5Game → {method} {path} "
                        f"(key={self._masked_key}, попытка {attempt + 1})"
                    )

                    async with session.request(
                        method,
                        path,
                        params=query_params,
                        json=json_body,
                    ) as resp:
                        status = resp.status
                        raw = await resp.read()
                        try:
                            data = _json.loads(raw.decode("utf-8", errors="replace"))
                        except (ValueError, UnicodeDecodeError) as parse_err:
                            raise C5GameAPIError(
                                f"Ошибка парсинга ответа: {parse_err} (status={status}, body={raw[:200]!r})",
                                status_code=status,
                            ) from parse_err

                        logger.debug(
                            f"C5Game ← {status} {path} "
                            f"(success={data.get('success')}, "
                            f"code={data.get('errorCode')})"
                        )

                        if status == 429:
                            delay = RETRY_429.get_delay(attempt)
                            logger.warning(
                                f"C5Game 429 Rate Limit {path} — "
                                f"ожидание {delay:.1f}с (попытка {attempt + 1})"
                            )
                            if attempt < RETRY_429.max_retries:
                                await asyncio.sleep(delay)
                                continue
                            raise C5GameAPIError(
                                f"Rate limit после {attempt + 1} попыток",
                                status_code=429,
                            )

                        if status >= 500:
                            delay = retries.get_delay(attempt)
                            logger.warning(
                                f"C5Game {status} {path} — "
                                f"retry через {delay:.1f}с (попытка {attempt + 1})"
                            )
                            if attempt < retries.max_retries:
                                await asyncio.sleep(delay)
                                continue
                            raise C5GameAPIError(
                                f"Серверная ошибка {status} после {attempt + 1} попыток",
                                status_code=status,
                                response_data=data,
                            )

                        if status >= 400:
                            raise C5GameAPIError(
                                f"HTTP {status}: {data.get('errorMsg', 'нет описания')}",
                                status_code=status,
                                response_data=data,
                            )

                        if not data.get("success", False):
                            error_msg = data.get("errorMsg", "нет описания")
                            error_code = data.get("errorCode", -1)
                            raise C5GameAPIError(
                                f"API [{error_code}]: {error_msg}",
                                status_code=status,
                                error_code=error_code,
                                response_data=data,
                            )

                        return data.get("data") or data

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_error = e
                    delay = retries.get_delay(attempt)
                    logger.error(
                        f"C5Game сетевая ошибка {path}: {e} — "
                        f"retry через {delay:.1f}с (попытка {attempt + 1})"
                    )
                    if attempt < retries.max_retries:
                        await asyncio.sleep(delay)
                        continue
                    raise

        raise last_error or C5GameAPIError("Исчерпаны попытки запроса")

    async def _raw_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> dict[str, Any]:
        """Выполнить запрос по произвольному пути (для path-параметров в URL).

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
                    async with session.request(
                        method, path, params=query_params, json=json_body,
                    ) as resp:
                        raw_bytes = await resp.read()
                        raw_text = raw_bytes.decode("utf-8")

                        # Rate limit — текстовый ответ "Too Many Request."
                        if resp.status == 429 or (
                            resp.status == 200 and "Too Many" in raw_text[:50]
                        ):
                            retry_delay = RETRY_429.get_delay(attempt)
                            logger.warning(
                                f"C5Game rate limit {path} — "
                                f"ожидание {retry_delay:.1f}с (попытка {attempt + 1})"
                            )
                            if attempt >= retries.max_retries:
                                raise C5GameAPIError(
                                    f"Rate limit после {attempt + 1} попыток",
                                    status_code=429,
                                )
                            # sleep после выхода из semaphore

                        if retry_delay is None:
                            raw = _json.loads(raw_text)
                            if not isinstance(raw, dict):
                                raise C5GameAPIError(
                                    f"Ответ не dict: {str(raw)[:100]}",
                                    status_code=resp.status,
                                )

                            logger.debug(
                                f"C5Game ← {resp.status} {path} "
                                f"(success={raw.get('success')})"
                            )

                            if resp.status >= 500:
                                retry_delay = retries.get_delay(attempt)
                                logger.warning(
                                    f"C5Game {resp.status} {path} — "
                                    f"retry через {retry_delay:.1f}с (попытка {attempt + 1})"
                                )
                                if attempt >= retries.max_retries:
                                    raise C5GameAPIError(
                                        f"Серверная ошибка {resp.status}",
                                        status_code=resp.status,
                                        response_data=raw,
                                    )

                            if retry_delay is None:
                                if not raw.get("success", False):
                                    raise C5GameAPIError(
                                        f"API [{raw.get('errorCode')}]: {raw.get('errorMsg')}",
                                        status_code=resp.status,
                                        error_code=raw.get("errorCode"),
                                        response_data=raw,
                                    )
                                return raw.get("data") or raw

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    retry_delay = retries.get_delay(attempt)
                    logger.error(
                        f"C5Game сетевая ошибка {path}: {e} — "
                        f"retry через {retry_delay:.1f}с (попытка {attempt + 1})"
                    )
                    if attempt >= retries.max_retries:
                        raise

            # Retry sleep вне semaphore
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)

        raise C5GameAPIError("Исчерпаны попытки запроса")

    # ──────────────────────────────────────────────────────
    # Аккаунт
    # ──────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """Баланс аккаунта.

        Returns:
            {"userId": str, "balance": float}
        """
        return await self._request("balance")

    # ──────────────────────────────────────────────────────
    # Рыночный листинг (чужие лоты для покупки)
    # ──────────────────────────────────────────────────────

    async def search_market(
        self,
        game_id: int = 570,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Список лотов на рынке для покупки (Query Item Sell).

        Путь: POST /merchant/market/v2/products/{gameId}
        gameId=570 для Dota 2.

        Returns:
            {"total": str, "pages": int, "list": [{id, price, itemName, marketHashName, ...}]}
            Поле ``id`` из каждого лота используется как itemId для quick_buy().
        """
        path = f"/merchant/market/v2/products/{game_id}"
        return await self._raw_request(
            "POST", path,
            json_body={"pageNum": page, "pageSize": page_size},
        )

    # ──────────────────────────────────────────────────────
    # Наши лоты (Sale — что мы выставили на продажу)
    # ──────────────────────────────────────────────────────

    async def get_sale_list(
        self,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Список наших активных лотов на продажу.

        Args:
            page: номер страницы.
            limit: кол-во на странице (макс. 50).
        """
        return await self._request(
            "sale_search",
            params={"page": page, "limit": limit},
        )

    async def create_sale(
        self,
        asset_id: str,
        price: float,
        game_id: int = 570,
    ) -> dict[str, Any]:
        """Выставить предмет на продажу.

        Args:
            asset_id: ID предмета из Steam-инвентаря.
            price: цена в CNY.
            game_id: ID игры (570 = Dota 2).
        """
        return await self._request(
            "sale_create",
            json_body={"assetId": asset_id, "price": price, "gameId": game_id},
        )

    async def modify_sale_price(
        self,
        item_id: str,
        price: float,
    ) -> dict[str, Any]:
        """Изменить цену выставленного лота.

        Args:
            item_id: ID лота (из get_sale_list).
            price: новая цена в CNY.
        """
        return await self._request(
            "sale_modify",
            json_body={"itemId": item_id, "price": price},
        )

    async def cancel_sale(self, item_id: str) -> dict[str, Any]:
        """Снять лот с продажи (Off Sell).

        Args:
            item_id: ID лота (из get_sale_list).
        """
        return await self._request(
            "sale_cancel",
            json_body={"itemId": item_id},
        )

    # ──────────────────────────────────────────────────────
    # Цены по hashName
    # ──────────────────────────────────────────────────────

    async def get_prices_batch(
        self,
        hash_names: list[str],
        game_id: int = 570,
    ) -> dict[str, Any]:
        """Пакетный запрос цен по hashName (Price Query).

        Args:
            hash_names: список Steam Market Hash Name.
            game_id: ID игры (570 = Dota 2).

        Returns:
            Словарь hashName → {price, count, ...}
        """
        return await self._request(
            "price_batch",
            json_body={"hashNames": hash_names, "gameId": game_id},
        )

    # ──────────────────────────────────────────────────────
    # Заказы
    # ──────────────────────────────────────────────────────

    async def get_buyer_orders(
        self,
        page: int = 1,
        limit: int = 50,
        status: int | None = None,
    ) -> dict[str, Any]:
        """Список заказов покупателя (Buyer Orders).

        Args:
            page: номер страницы.
            limit: кол-во на странице.
            status: фильтр по статусу (опционально).
        """
        body: dict[str, Any] = {"page": page, "limit": limit}
        if status is not None:
            body["status"] = status
        return await self._request("order_buyer", json_body=body)

    async def get_seller_orders(
        self,
        page: int = 1,
        limit: int = 50,
        status: int | None = None,
    ) -> dict[str, Any]:
        """Список заказов продавца (Seller Orders).

        Args:
            page: номер страницы.
            limit: кол-во на странице.
            status: фильтр по статусу (опционально).
        """
        body: dict[str, Any] = {"page": page, "limit": limit}
        if status is not None:
            body["status"] = status
        return await self._request("order_seller", json_body=body)

    async def get_order_detail(self, order_id: str) -> dict[str, Any]:
        """Детали заказа покупателя (Buyer Order Detail).

        Args:
            order_id: ID заказа.
        """
        return await self._request(
            "order_detail",
            params={"orderId": order_id},
        )

    # ──────────────────────────────────────────────────────
    # Покупка
    # ──────────────────────────────────────────────────────

    async def quick_buy(
        self,
        item_id: str,
        max_price: float,
        out_trade_no: str,
        trade_url: str,
    ) -> dict[str, Any]:
        """Быстрая покупка по ID лота (Quick Buy).

        ⚠️ Реальная покупка! Использовать только при dry_run=False.
        ⚠️ Требует активации 购买权限 от поддержки C5Game (ошибка 820001).

        Args:
            item_id: ID лота из search_market() (поле ``id``).
            max_price: максимальная цена в CNY (защита от изменения цены).
            out_trade_no: уникальный ID заказа от нашей стороны (UUID).
            trade_url: Steam Trade URL покупателя.
        """
        return await self._request(
            "trade_quick",
            json_body={
                "itemId": item_id,
                "maxPrice": max_price,
                "outTradeNo": out_trade_no,
                "tradeUrl": trade_url,
            },
        )

    async def normal_buy(
        self,
        product_id: str,
        buy_price: float,
        out_trade_no: str,
        trade_url: str,
    ) -> dict[str, Any]:
        """Обычная покупка по цене (Normal Buy).

        ⚠️ Реальная покупка! Использовать только при dry_run=False.
        ⚠️ Требует активации 购买权限 от поддержки C5Game (ошибка 820001).

        Args:
            product_id: ID лота из search_market() (поле ``id``).
            buy_price: точная цена в CNY.
            out_trade_no: уникальный ID заказа от нашей стороны (UUID).
            trade_url: Steam Trade URL покупателя.
        """
        return await self._request(
            "trade_normal",
            json_body={
                "productId": product_id,
                "buyPrice": buy_price,
                "outTradeNo": out_trade_no,
                "tradeUrl": trade_url,
            },
        )


class C5GameAPIError(Exception):
    """Ошибка API C5Game."""

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
