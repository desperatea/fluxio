"""HTTP-клиент для Steam Market API.

Эндпоинты:
- priceoverview (публичный): текущая median_price, lowest_price, volume
- pricehistory (требует steamLoginSecure): полная история цен за ~30 дней

Rate limit Steam: ~1 запрос в 3 секунды (агрессивнее → временный IP-бан).
"""

from __future__ import annotations

import asyncio
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from urllib.parse import quote as url_quote

import aiohttp
from loguru import logger

from fluxio.config import config
from fluxio.utils.rate_limiter import RetryConfig, TokenBucketRateLimiter

# Steam Market rate limit: ~1 запрос в 5 секунд на канал (консервативно)
_STEAM_RATE = 0.2
_STEAM_BURST = 1

# Retry для Steam — больше попыток с длинными паузами
RETRY_STEAM = RetryConfig(max_retries=4, base_delay=10.0, max_delay=60.0, backoff_factor=2.0)


class SteamClient:
    """Асинхронный HTTP-клиент для Steam Community Market."""

    def __init__(self) -> None:
        self._sessions: list[aiohttp.ClientSession | None] = []

        # Собираем все пары cookies из .env (STEAM_LOGIN_SECURE, STEAM_LOGIN_SECURE2, ...)
        self._cookie_sets: list[dict[str, str]] = []
        for suffix in ("", "2", "3", "4", "5"):
            login_secure = os.getenv(f"STEAM_LOGIN_SECURE{suffix}", "").strip("'\"")
            session_id = os.getenv(f"SESSION_ID{suffix}", "").strip("'\"")
            browser_id = os.getenv(f"BROUSER_ID{suffix}", "").strip("'\"")
            if login_secure:
                cookies: dict[str, str] = {"steamLoginSecure": login_secure}
                if session_id:
                    cookies["sessionid"] = session_id
                if browser_id:
                    cookies["browserid"] = browser_id
                self._cookie_sets.append(cookies)

        # Основные cookies (первый набор) для pricehistory
        self._cookies = self._cookie_sets[0] if self._cookie_sets else {}
        self._has_auth = bool(self._cookies)
        self._auth_fail_count = 0  # Счётчик неудачных pricehistory (авто-отключение)
        if not self._has_auth:
            logger.warning(
                "STEAM_LOGIN_SECURE не задан — pricehistory недоступен, "
                "используется только priceoverview"
            )

        # Прокси для обхода Steam rate limit — ротация между несколькими
        self._proxy_urls: list[str | None] = []
        self._proxy_index = 0

        # 1. Загружаем прокси из файла (proxy.txt.txt или STEAM_PROXY_FILE)
        proxy_file = os.getenv("STEAM_PROXY_FILE", "").strip("'\"")
        if not proxy_file:
            # Ищем proxy.txt.txt в корне проекта
            from fluxio.config import PROJECT_ROOT
            for candidate in ("proxy.txt.txt", "proxy.txt", "proxies.txt"):
                path = PROJECT_ROOT / candidate
                if path.exists():
                    proxy_file = str(path)
                    break

        if proxy_file and os.path.isfile(proxy_file):
            with open(proxy_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    proxy_url = self._parse_proxy(line)
                    if proxy_url:
                        self._proxy_urls.append(proxy_url)
            logger.info(f"Загружено {len(self._proxy_urls)} прокси из {proxy_file}")

        # 2. Дополняем прокси из .env (если файл не найден или пуст)
        if not self._proxy_urls:
            proxy_env_keys = ["STEAM_PROXY", "HTTP_PROXY", "HTTP_PROXY2", "HTTPS_PROXY"]
            seen_hosts: set[str] = set()
            for key in proxy_env_keys:
                raw_proxy = os.getenv(key, "").strip("'\"")
                if not raw_proxy:
                    continue
                proxy_url = self._parse_proxy(raw_proxy)
                if proxy_url:
                    host_port = raw_proxy.split(":")[0] + ":" + raw_proxy.split(":")[1] if ":" in raw_proxy else raw_proxy
                    if host_port not in seen_hosts:
                        seen_hosts.add(host_port)
                        self._proxy_urls.append(proxy_url)

        # Прямое подключение тоже как канал
        self._proxy_urls.append(None)

        # Для обратной совместимости
        self._proxy_url = self._proxy_urls[0] if self._proxy_urls else None

        # Инициализируем список сессий (по одной на канал)
        self._sessions = [None] * len(self._proxy_urls)

        # Отдельный rate limiter на каждый прокси/канал
        # Если много каналов (файл прокси) — можно быстрее на канал
        channel_count = len(self._proxy_urls)
        rate_per_channel = 0.33 if channel_count > 10 else _STEAM_RATE  # 1 req/3s vs 1/5s
        self._proxy_rate_limiters = [
            TokenBucketRateLimiter(rate=rate_per_channel, burst=_STEAM_BURST, name=f"steam_ch{i}")
            for i in range(channel_count)
        ]
        # Семафор: ограничиваем параллельность чтобы не перегрузить
        max_parallel = min(channel_count, 25)
        self._semaphore = asyncio.Semaphore(max_parallel)

        proxy_count = len([p for p in self._proxy_urls if p is not None])
        logger.info(
            f"Steam: {proxy_count} прокси + direct = {len(self._proxy_urls)} каналов, "
            f"{len(self._cookie_sets)} наборов cookies"
        )

    @staticmethod
    def _parse_proxy(raw: str) -> str | None:
        """Преобразовать строку прокси в URL для aiohttp."""
        raw = raw.strip("'\"")
        if not raw:
            return None
        if raw.startswith(("http://", "https://", "socks")):
            return raw
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
        elif len(parts) == 2:
            return f"http://{raw}"
        return None

    def _next_proxy(self) -> tuple[int, str | None, TokenBucketRateLimiter]:
        """Выбрать следующий прокси (round-robin) и его rate limiter."""
        idx = self._proxy_index % len(self._proxy_urls)
        self._proxy_index += 1
        return idx, self._proxy_urls[idx], self._proxy_rate_limiters[idx]

    async def _get_session(self, channel_idx: int = 0) -> aiohttp.ClientSession:
        """Получить или создать aiohttp-сессию для канала."""
        if channel_idx >= len(self._sessions):
            channel_idx = 0
        session = self._sessions[channel_idx]
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            # Каждый канал получает свой набор cookies (если есть)
            cookies = self._cookie_sets[channel_idx % len(self._cookie_sets)] if self._cookie_sets else {}
            session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                cookies=cookies,
            )
            self._sessions[channel_idx] = session
        return session

    async def close(self) -> None:
        """Закрыть все HTTP-сессии."""
        for i, session in enumerate(self._sessions):
            if session and not session.closed:
                await session.close()
        self._sessions = [None] * len(self._proxy_urls)
        logger.debug("Steam HTTP-сессии закрыты")

    async def _request_json(
        self,
        url: str,
        retry: RetryConfig | None = None,
        use_proxy: bool = True,
    ) -> dict[str, Any] | None:
        """GET-запрос к Steam с rate limiting, ротацией прокси и retry."""
        retries = retry or RETRY_STEAM

        # Выбираем прокси-канал
        if use_proxy:
            ch_idx, proxy, rate_limiter = self._next_proxy()
        else:
            ch_idx = len(self._proxy_urls) - 1  # прямой канал (последний)
            proxy = None
            rate_limiter = self._proxy_rate_limiters[-1]

        session = await self._get_session(ch_idx)

        for attempt in range(retries.max_retries + 1):
            retry_delay: float | None = None

            async with self._semaphore:
                await rate_limiter.acquire()

                try:
                    logger.debug(f"Steam → GET {url[:80]}... (попытка {attempt + 1})")
                    async with session.get(url, proxy=proxy) as resp:
                        if resp.status == 429:
                            delay = retries.get_delay(attempt)
                            logger.warning(
                                f"Steam 429 Rate Limit — ожидание {delay:.0f}с "
                                f"(попытка {attempt + 1})"
                            )
                            if attempt < retries.max_retries:
                                retry_delay = delay
                            else:
                                return None

                        elif resp.status >= 400:
                            logger.error(f"Steam HTTP {resp.status}: {url[:80]}")
                            return None

                        else:
                            # Проверяем что вернулся JSON, а не HTML (редирект на логин)
                            ct = resp.content_type or ""
                            if "json" not in ct and "javascript" not in ct:
                                logger.warning(
                                    f"Steam вернул не-JSON ({ct}) — "
                                    "возможно, cookies устарели"
                                )
                                return None

                            return await resp.json(content_type=None)

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    delay = retries.get_delay(attempt)
                    logger.error(
                        f"Steam сетевая ошибка: {e} — "
                        f"retry через {delay:.0f}с (попытка {attempt + 1})"
                    )
                    if attempt < retries.max_retries:
                        retry_delay = delay
                    else:
                        return None

            # Sleep вне семафора, чтобы не блокировать других
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)

        return None

    # ──────────────────────────────────────────────────────
    # search/render — листинг всех предметов с пагинацией
    # ──────────────────────────────────────────────────────

    async def search_market(
        self,
        app_id: int = 570,
        start: int = 0,
        count: int = 100,
        sort_column: str = "name",
        sort_dir: str = "asc",
        query: str = "",
    ) -> dict[str, Any] | None:
        """Поиск предметов на Steam Market с пагинацией.

        Args:
            app_id: ID игры (570=Dota2).
            start: смещение для пагинации.
            count: кол-во результатов (макс 100).
            sort_column: поле сортировки (name, price, quantity).
            sort_dir: направление (asc, desc).
            query: поисковый запрос (пустой = все предметы).

        Returns:
            {"success": true, "total_count": N, "results": [...]} или None.
        """
        url = (
            f"https://steamcommunity.com/market/search/render/"
            f"?appid={app_id}&norender=1"
            f"&start={start}&count={count}"
            f"&sort_column={sort_column}&sort_dir={sort_dir}"
            f"&search_descriptions=0"
        )
        if query:
            url += f"&query={url_quote(query, safe='')}"

        data = await self._request_json(url)
        if data and data.get("success"):
            return data
        return None

    # ──────────────────────────────────────────────────────
    # itemordershistogram — ордера на покупку/продажу
    # ──────────────────────────────────────────────────────

    async def get_item_nameid(
        self,
        market_hash_name: str,
        app_id: int = 570,
    ) -> int | None:
        """Получить item_nameid со страницы листинга (нужен для ордеров).

        Парсит HTML страницы предмета, ищет Market_LoadOrderSpread(XXXXXX).
        """
        url = (
            f"https://steamcommunity.com/market/listings/"
            f"{app_id}/{url_quote(market_hash_name, safe='')}"
        )
        ch_idx, proxy, rate_limiter = self._next_proxy()
        session = await self._get_session(ch_idx)

        async with self._semaphore:
            await rate_limiter.acquire()
            try:
                async with session.get(url, proxy=proxy) as resp:
                    if resp.status != 200:
                        logger.warning(f"Steam listing page HTTP {resp.status}: {market_hash_name}")
                        return None
                    html = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Ошибка загрузки страницы листинга: {e}")
                return None

        # Ищем Market_LoadOrderSpread( 176208497 );
        match = re.search(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)", html)
        if match:
            return int(match.group(1))

        # Альтернатива: ItemActivityTicker.Start( 176208497 );
        match = re.search(r"ItemActivityTicker\.Start\(\s*(\d+)\s*\)", html)
        if match:
            return int(match.group(1))

        logger.debug(f"item_nameid не найден для {market_hash_name}")
        return None

    async def get_item_orders_histogram(
        self,
        item_nameid: int,
        currency: int = 1,
    ) -> dict[str, Any] | None:
        """Получить ордера на покупку и продажу предмета.

        Args:
            item_nameid: ID предмета (из get_item_nameid).
            currency: 1=USD.

        Returns:
            {"success": 1, "buy_order_graph": [...], "sell_order_graph": [...],
             "highest_buy_order": "123", "lowest_sell_order": "456", ...}
        """
        url = (
            f"https://steamcommunity.com/market/itemordershistogram"
            f"?country=US&language=english&currency={currency}"
            f"&item_nameid={item_nameid}&two_factor=0"
            f"&norender=1"
        )
        data = await self._request_json(url)
        if data and data.get("success") == 1:
            return data
        return None

    # ──────────────────────────────────────────────────────
    # priceoverview — публичный, без авторизации
    # ──────────────────────────────────────────────────────

    async def get_price_overview(
        self,
        market_hash_name: str,
        app_id: int = 570,
        currency: int = 1,
    ) -> dict[str, Any] | None:
        """Текущая цена и объём продаж за 24ч.

        Args:
            market_hash_name: точное имя предмета в Steam Market.
            app_id: ID игры (570=Dota2, 730=CS2).
            currency: 1=USD, 23=CNY.

        Returns:
            {"lowest_price": "$0.03", "median_price": "$0.03", "volume": "8"}
            или None при ошибке.
        """
        url = (
            f"https://steamcommunity.com/market/priceoverview/"
            f"?appid={app_id}&currency={currency}"
            f"&market_hash_name={url_quote(market_hash_name, safe='')}"
        )
        data = await self._request_json(url)
        if data and data.get("success"):
            return data
        return None

    # ──────────────────────────────────────────────────────
    # pricehistory — требует steamLoginSecure
    # ──────────────────────────────────────────────────────

    async def get_price_history(
        self,
        market_hash_name: str,
        app_id: int = 570,
        currency: int = 1,
    ) -> list[list[Any]] | None:
        """Полная история цен предмета на Steam Market.

        Требует cookie steamLoginSecure.

        Args:
            market_hash_name: точное имя предмета.
            app_id: ID игры.
            currency: 1=USD.

        Returns:
            Список [[date_str, price_float, volume_str], ...] или None.
        """
        if not self._has_auth:
            logger.debug(
                f"pricehistory пропущен (нет auth): {market_hash_name}"
            )
            return None

        url = (
            f"https://steamcommunity.com/market/pricehistory/"
            f"?appid={app_id}&currency={currency}"
            f"&market_hash_name={url_quote(market_hash_name, safe='')}"
        )
        # pricehistory требует авторизацию — не через прокси (IP привязан к cookies)
        data = await self._request_json(url, use_proxy=False)
        if data and data.get("success"):
            self._auth_fail_count = 0
            return data.get("prices", [])

        # Автодетект протухших cookies: 3 подряд неудачи → отключаем pricehistory
        self._auth_fail_count += 1
        if self._auth_fail_count >= 3 and self._has_auth:
            self._has_auth = False
            logger.warning(
                "Steam cookies протухли (3 подряд ошибки pricehistory) — "
                "pricehistory отключён, используется только priceoverview"
            )
        return None

    # ──────────────────────────────────────────────────────
    # Расчёт медианной цены за N дней
    # ──────────────────────────────────────────────────────

    async def get_median_price(
        self,
        market_hash_name: str,
        days: int = 30,
        app_id: int = 570,
    ) -> SteamPriceData | None:
        """Медианная цена и объём продаж за последние N дней.

        Стратегия:
        1. Если есть auth → pricehistory (полные данные)
        2. Fallback → priceoverview (только текущие данные)

        Returns:
            SteamPriceData или None если предмет не найден.
        """
        # Попробовать pricehistory (полные данные)
        history = await self.get_price_history(market_hash_name, app_id=app_id)
        if history:
            result = self._calc_from_history(history, days)
            if result:
                logger.debug(
                    f"Steam цена [{market_hash_name}]: "
                    f"медиана=${result.median_price_usd:.4f}, "
                    f"продаж={result.sales_count} за {days}д"
                )
                return result

        # Fallback: priceoverview
        overview = await self.get_price_overview(market_hash_name, app_id=app_id)
        if overview:
            result = self._parse_overview(overview)
            if result:
                logger.debug(
                    f"Steam цена (overview) [{market_hash_name}]: "
                    f"медиана=${result.median_price_usd:.4f}, "
                    f"объём={result.sales_count}/24ч"
                )
                return result

        logger.debug(f"Steam: предмет не найден [{market_hash_name}]")
        return None

    def _calc_from_history(
        self,
        history: list[list[Any]],
        days: int,
    ) -> SteamPriceData | None:
        """Вычислить медиану и объём из массива pricehistory."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        prices: list[float] = []
        total_volume = 0

        for entry in history:
            if len(entry) < 3:
                continue
            date_str, price, volume_str = entry[0], entry[1], entry[2]

            # Парсим дату: "Mar 07 2026 12: +0"
            try:
                clean = date_str.split(": +")[0].strip()
                dt = datetime.strptime(clean, "%b %d %Y %H")
            except (ValueError, IndexError):
                continue
            dt = dt.replace(tzinfo=timezone.utc)

            if dt < cutoff:
                continue

            vol = int(volume_str.replace(",", "")) if isinstance(volume_str, str) else int(volume_str)
            prices.extend([float(price)] * vol)
            total_volume += vol

        if not prices:
            return None

        return SteamPriceData(
            median_price_usd=statistics.median(prices),
            lowest_price_usd=min(prices),
            sales_count=total_volume,
            source="pricehistory",
        )

    @staticmethod
    def _parse_price_string(price_str: str) -> float | None:
        """Извлечь числовое значение из строки цены Steam ('$0.03', '¥0.21').

        Поддерживает форматы: $0.03, $1,234.56, 1.234,56 (EU), ¥0.21
        """
        # Убираем символы валют и пробелы
        cleaned = re.sub(r"[^\d.,]", "", price_str)
        if not cleaned:
            return None

        # Определяем формат: если есть и точка и запятая
        if "," in cleaned and "." in cleaned:
            # $1,234.56 (US) или 1.234,56 (EU) — последний разделитель = десятичный
            last_comma = cleaned.rfind(",")
            last_dot = cleaned.rfind(".")
            if last_dot > last_comma:
                # US: 1,234.56 → убираем запятые
                cleaned = cleaned.replace(",", "")
            else:
                # EU: 1.234,56 → убираем точки, запятая → точка
                cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Только запятая: 1,234 (тысячный) или 0,03 (десятичный EU)
            parts = cleaned.split(",")
            if len(parts[-1]) == 3 and len(parts) > 1:
                # Тысячный разделитель: 1,234
                cleaned = cleaned.replace(",", "")
            else:
                # Десятичный: 0,03
                cleaned = cleaned.replace(",", ".")

        try:
            return float(cleaned)
        except ValueError:
            return None

    def _parse_overview(
        self,
        data: dict[str, Any],
    ) -> SteamPriceData | None:
        """Парсить ответ priceoverview."""
        median_str = data.get("median_price", "")
        lowest_str = data.get("lowest_price", "")
        volume_str = data.get("volume", "0")

        median = self._parse_price_string(median_str) if median_str else None
        lowest = self._parse_price_string(lowest_str) if lowest_str else None

        if median is None and lowest is None:
            return None

        price = median or lowest or 0.0
        vol = int(volume_str.replace(",", "")) if volume_str else 0

        return SteamPriceData(
            median_price_usd=price,
            lowest_price_usd=lowest or price,
            sales_count=vol,
            source="priceoverview",
        )


class SteamPriceData:
    """Результат запроса цены предмета на Steam Market."""

    __slots__ = ("median_price_usd", "lowest_price_usd", "sales_count", "source")

    def __init__(
        self,
        median_price_usd: float,
        lowest_price_usd: float,
        sales_count: int,
        source: str,
    ) -> None:
        self.median_price_usd = median_price_usd
        self.lowest_price_usd = lowest_price_usd
        self.sales_count = sales_count
        self.source = source  # "pricehistory" | "priceoverview"

    def __repr__(self) -> str:
        return (
            f"SteamPriceData(median=${self.median_price_usd:.4f}, "
            f"lowest=${self.lowest_price_usd:.4f}, "
            f"sales={self.sales_count}, source={self.source})"
        )
