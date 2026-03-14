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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from urllib.parse import quote as url_quote

import aiohttp
from loguru import logger

from fluxio.config import config
from fluxio.utils.rate_limiter import RetryConfig, TokenBucketRateLimiter


@dataclass
class _AuthChannel:
    """Канал авторизации для pricehistory (куки + выделенный прокси)."""

    cookies: dict[str, str]
    proxy_url: str | None
    rate_limiter: TokenBucketRateLimiter
    session: aiohttp.ClientSession | None = field(default=None, repr=False)
    has_auth: bool = True
    fail_count: int = 0
    label: str = ""
    # Коэффициент конвертации валюты аккаунта в USD (auto-detected)
    currency_rate: float | None = None

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

        # Auth-каналы для pricehistory: каждый = (куки + прокси), IP привязан
        self._auth_channels: list[_AuthChannel] = []
        self._auth_index = 0
        for suffix in ("", "2", "3", "4", "5"):
            login_secure = os.getenv(f"STEAM_LOGIN_SECURE{suffix}", "").strip("'\"")
            if not login_secure:
                continue
            session_id = os.getenv(f"SESSION_ID{suffix}", "").strip("'\"")
            browser_id = os.getenv(f"BROUSER_ID{suffix}", "").strip("'\"")
            proxy_raw = os.getenv(f"STEAM_PROXY{suffix}", "").strip("'\"")

            cookies: dict[str, str] = {"steamLoginSecure": login_secure}
            if session_id:
                cookies["sessionid"] = session_id
            if browser_id:
                cookies["browserid"] = browser_id

            proxy_url = self._parse_proxy(proxy_raw) if proxy_raw else None
            label = suffix or "1"
            self._auth_channels.append(_AuthChannel(
                cookies=cookies,
                proxy_url=proxy_url,
                rate_limiter=TokenBucketRateLimiter(
                    rate=0.2, burst=1, name=f"steam_auth_{label}",
                ),
                label=label,
            ))

        self._has_auth = bool(self._auth_channels)
        if self._auth_channels:
            logger.info(
                f"Steam: {len(self._auth_channels)} auth-каналов для pricehistory"
            )
        else:
            logger.warning(
                "STEAM_LOGIN_SECURE не задан — pricehistory недоступен, "
                "используется только priceoverview"
            )

        # Прокси для обхода Steam rate limit — ротация между несколькими
        self._proxy_urls: list[str | None] = []
        self._proxy_index = 0
        # Счётчик 429 ошибок по каждому каналу (индекс → количество)
        self._proxy_429_counts: dict[int, int] = {}
        # Путь к файлу прокси (для перезаписи после валидации)
        self._proxy_file: str | None = None
        # Маппинг proxy_url → оригинальная строка из файла
        self._proxy_raw_lines: dict[str, str] = {}

        # 1. Загружаем прокси из файла
        proxy_file = os.getenv("STEAM_PROXY_FILE", "").strip("'\"")
        if not proxy_file:
            from fluxio.config import PROJECT_ROOT
            for candidate in ("proxy.txt.txt", "proxy.txt", "proxies.txt"):
                path = PROJECT_ROOT / candidate
                if path.exists():
                    proxy_file = str(path)
                    break

        if proxy_file and os.path.isfile(proxy_file):
            self._proxy_file = proxy_file
            with open(proxy_file, "r", encoding="utf-8") as f:
                for line in f:
                    raw_line = line.strip()
                    if not raw_line:
                        continue
                    proxy_url = self._parse_proxy(raw_line)
                    if proxy_url:
                        self._proxy_urls.append(proxy_url)
                        self._proxy_raw_lines[proxy_url] = raw_line
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

        # Каналы будут инициализированы в _init_channels()
        self._init_channels()

    def _init_channels(self) -> None:
        """Инициализировать каналы, rate limiters и семафор по текущему списку прокси."""
        # Прямое подключение тоже как канал (всегда последний)
        if self._proxy_urls and self._proxy_urls[-1] is None:
            pass  # direct уже добавлен
        else:
            self._proxy_urls.append(None)

        # Для обратной совместимости
        self._proxy_url = self._proxy_urls[0] if self._proxy_urls else None

        # Инициализируем список сессий (по одной на канал)
        self._sessions = [None] * len(self._proxy_urls)

        # Отдельный rate limiter на каждый прокси/канал
        channel_count = len(self._proxy_urls)
        rate_per_channel = 0.33 if channel_count > 10 else _STEAM_RATE
        self._proxy_rate_limiters = [
            TokenBucketRateLimiter(rate=rate_per_channel, burst=_STEAM_BURST, name=f"steam_ch{i}")
            for i in range(channel_count)
        ]
        # Динамический семафор: параллельность зависит от кол-ва прокси
        max_parallel = min(channel_count, 50)
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._proxy_index = 0
        self._proxy_429_counts = {}

        proxy_count = len([p for p in self._proxy_urls if p is not None])
        logger.info(
            f"Steam: {proxy_count} прокси + direct = {channel_count} каналов, "
            f"параллельность={max_parallel}, "
            f"rate={rate_per_channel:.2f} req/s на канал"
        )

    def proxy_stats(self) -> dict[str, Any]:
        """Статистика прокси для дашборда."""
        total = len(self._proxy_urls)
        # Последний канал — direct (без прокси)
        proxy_count = len([p for p in self._proxy_urls if p is not None])
        # «Плохие» прокси — каналы с 429 ошибками (порог: >=3)
        bad_proxies: list[dict[str, Any]] = []
        for idx, count in sorted(self._proxy_429_counts.items()):
            if count >= 3:
                proxy_url = self._proxy_urls[idx] if idx < total else None
                # Маскируем пароль в URL
                label = self._mask_proxy(proxy_url) if proxy_url else "direct"
                bad_proxies.append({"channel": idx, "label": label, "count_429": count})
        return {
            "total_proxies": proxy_count,
            "total_channels": total,
            "bad_proxies": bad_proxies,
            "counts_429": dict(self._proxy_429_counts),
        }

    async def validate_proxies(self) -> None:
        """Проверить все прокси и удалить нерабочие из файла.

        Делает тестовый запрос через каждый прокси к Steam.
        Нерабочие прокси удаляются из self._proxy_urls и из файла proxy.txt.
        После валидации переинициализирует каналы.
        """
        if not any(p for p in self._proxy_urls if p is not None):
            logger.info("Прокси не загружены — валидация пропущена")
            return

        test_url = (
            "https://steamcommunity.com/market/priceoverview/"
            "?appid=570&currency=1"
            "&market_hash_name=Mann%20Co.%20Supply%20Crate%20Key"
        )
        timeout = aiohttp.ClientTimeout(total=15)
        good_proxies: list[str] = []
        bad_proxies: list[str] = []

        # Проверяем все прокси параллельно (батчами по 20)
        proxies_to_check = [p for p in self._proxy_urls if p is not None]
        logger.info(f"Валидация {len(proxies_to_check)} прокси...")

        sem = asyncio.Semaphore(20)

        async def _check_one(proxy_url: str) -> bool:
            async with sem:
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as sess:
                        async with sess.get(test_url, proxy=proxy_url) as resp:
                            # 200 или 429 — прокси работает (429 = rate limit, но соединение есть)
                            if resp.status in (200, 429):
                                return True
                            logger.warning(
                                f"Прокси {self._mask_proxy(proxy_url)} — HTTP {resp.status}"
                            )
                            return False
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    logger.warning(
                        f"Прокси {self._mask_proxy(proxy_url)} — ошибка: {type(e).__name__}: {e}"
                    )
                    return False

        tasks = [_check_one(p) for p in proxies_to_check]
        results = await asyncio.gather(*tasks)

        for proxy_url, ok in zip(proxies_to_check, results):
            if ok:
                good_proxies.append(proxy_url)
            else:
                bad_proxies.append(proxy_url)

        logger.info(
            f"Валидация завершена: {len(good_proxies)} рабочих, "
            f"{len(bad_proxies)} нерабочих"
        )

        # Удаляем плохие прокси из файла
        if bad_proxies and self._proxy_file:
            bad_raw_lines = {self._proxy_raw_lines.get(p) for p in bad_proxies}
            try:
                with open(self._proxy_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                new_lines = [l for l in lines if l.strip() and l.strip() not in bad_raw_lines]
                with open(self._proxy_file, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                logger.info(
                    f"Удалено {len(bad_proxies)} нерабочих прокси из {self._proxy_file}, "
                    f"осталось {len(new_lines)} строк"
                )
            except OSError as e:
                logger.error(f"Не удалось обновить файл прокси: {e}")

        # Переинициализируем каналы с рабочими прокси
        if bad_proxies:
            # Закрываем старые сессии
            await self.close()
            # Убираем direct (None) перед переинициализацией — _init_channels добавит его
            self._proxy_urls = good_proxies.copy()
            self._init_channels()

    @staticmethod
    def _mask_proxy(url: str) -> str:
        """Маскировать пароль в URL прокси для отображения."""
        # http://user:password@host:port → host:port
        if "@" in url:
            return url.split("@", 1)[1]
        return url.replace("http://", "").replace("https://", "")

    @staticmethod
    def _parse_proxy(raw: str) -> str | None:
        """Преобразовать строку прокси в URL для aiohttp.

        Поддерживаемые форматы:
        - http://user:pass@host:port (уже готовый URL)
        - host:port:user:password
        - host:port@user:password
        - host:port
        """
        raw = raw.strip("'\"")
        if not raw:
            return None
        if raw.startswith(("http://", "https://", "socks")):
            return raw
        # Формат host:port@user:password
        if "@" in raw:
            host_port, user_pass = raw.split("@", 1)
            if ":" in host_port and ":" in user_pass:
                host, port = host_port.rsplit(":", 1)
                user, password = user_pass.split(":", 1)
                return f"http://{user}:{password}@{host}:{port}"
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
        """Закрыть все HTTP-сессии (каналы прокси + auth-каналы)."""
        for i, session in enumerate(self._sessions):
            if session and not session.closed:
                await session.close()
        self._sessions = [None] * len(self._proxy_urls)

        for ch in self._auth_channels:
            if ch.session and not ch.session.closed:
                await ch.session.close()
            ch.session = None

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
                            self._proxy_429_counts[ch_idx] = self._proxy_429_counts.get(ch_idx, 0) + 1
                            delay = retries.get_delay(attempt)
                            logger.warning(
                                f"Steam 429 Rate Limit (канал {ch_idx}) — ожидание {delay:.0f}с "
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

    def _next_auth_channel(self) -> _AuthChannel | None:
        """Выбрать следующий живой auth-канал (round-robin)."""
        if not self._auth_channels:
            return None
        total = len(self._auth_channels)
        for _ in range(total):
            idx = self._auth_index % total
            self._auth_index += 1
            ch = self._auth_channels[idx]
            if ch.has_auth:
                return ch
        return None

    async def _get_auth_session(self, channel: _AuthChannel) -> aiohttp.ClientSession:
        """Получить или создать сессию для auth-канала."""
        if channel.session is None or channel.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            channel.session = aiohttp.ClientSession(
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
                cookies=channel.cookies,
            )
        return channel.session

    async def get_price_history(
        self,
        market_hash_name: str,
        app_id: int = 570,
        currency: int = 1,
    ) -> list[list[Any]] | None:
        """Полная история цен предмета на Steam Market.

        Требует cookie steamLoginSecure. Round-robin по auth-каналам
        (каждый канал = куки + выделенный прокси).

        Returns:
            Список [[date_str, price_float, volume_str], ...] или None.
        """
        channel = self._next_auth_channel()
        if channel is None:
            logger.debug(
                f"pricehistory пропущен (нет живых auth-каналов): {market_hash_name}"
            )
            return None

        url = (
            f"https://steamcommunity.com/market/pricehistory/"
            f"?appid={app_id}&currency={currency}"
            f"&market_hash_name={url_quote(market_hash_name, safe='')}"
        )

        session = await self._get_auth_session(channel)
        await channel.rate_limiter.acquire()

        try:
            logger.debug(
                f"Steam pricehistory [{market_hash_name}] "
                f"через auth-канал {channel.label}"
            )
            async with session.get(url, proxy=channel.proxy_url) as resp:
                if resp.status == 429:
                    logger.warning(
                        f"Steam pricehistory 429 (auth-канал {channel.label})"
                    )
                    return None

                if resp.status >= 400:
                    logger.error(
                        f"Steam pricehistory HTTP {resp.status} "
                        f"(auth-канал {channel.label})"
                    )
                    return None

                ct = resp.content_type or ""
                if "json" not in ct and "javascript" not in ct:
                    logger.warning(
                        f"Steam pricehistory не-JSON ({ct}) — "
                        f"auth-канал {channel.label}, возможно куки протухли"
                    )
                    channel.fail_count += 1
                    if channel.fail_count >= 3:
                        channel.has_auth = False
                        self._has_auth = any(
                            ch.has_auth for ch in self._auth_channels
                        )
                        logger.warning(
                            f"Auth-канал {channel.label} отключён "
                            f"(3 подряд ошибки pricehistory)"
                        )
                    return None

                data = await resp.json(content_type=None)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(
                f"Steam pricehistory сетевая ошибка "
                f"(auth-канал {channel.label}): {e}"
            )
            return None

        if data and data.get("success"):
            channel.fail_count = 0
            prices = data.get("prices", [])

            # Автодетект валюты: при первом запросе определяем курс
            if prices and channel.currency_rate is None:
                await self._detect_currency_rate(channel, market_hash_name, prices)

            # Конвертируем в USD если валюта не доллар
            if prices and channel.currency_rate is not None and channel.currency_rate != 1.0:
                prices = [
                    [entry[0], entry[1] / channel.currency_rate, *entry[2:]]
                    if len(entry) >= 2 else entry
                    for entry in prices
                ]

            return prices

        # Автодетект протухших cookies
        channel.fail_count += 1
        if channel.fail_count >= 3 and channel.has_auth:
            channel.has_auth = False
            self._has_auth = any(ch.has_auth for ch in self._auth_channels)
            logger.warning(
                f"Auth-канал {channel.label} отключён "
                f"(3 подряд ошибки pricehistory, куки протухли?)"
            )
        return None

    async def _detect_currency_rate(
        self,
        channel: _AuthChannel,
        hash_name: str,
        history: list[list[Any]],
    ) -> None:
        """Определить курс валюты аккаунта к USD.

        Сравниваем медиану из pricehistory с priceoverview (который всегда USD).
        Если разница > 2x — аккаунт в другой валюте, вычисляем коэффициент.
        """
        # Берём последние записи из history для сравнения
        recent_prices: list[float] = []
        for entry in history[-20:]:
            if len(entry) >= 2:
                try:
                    recent_prices.append(float(entry[1]))
                except (ValueError, TypeError):
                    continue

        if not recent_prices:
            channel.currency_rate = 1.0
            return

        history_median = statistics.median(recent_prices)

        # Получаем USD цену через priceoverview (публичный, всегда USD)
        overview = await self.get_price_overview(hash_name)
        if not overview:
            channel.currency_rate = 1.0
            logger.warning(
                f"Auth-канал {channel.label}: не удалось определить валюту "
                f"(priceoverview недоступен), считаем USD"
            )
            return

        overview_data = self._parse_overview(overview)
        if not overview_data or overview_data.median_price_usd <= 0:
            channel.currency_rate = 1.0
            return

        usd_price = overview_data.median_price_usd
        ratio = history_median / usd_price

        if ratio < 1.5:
            # Валюта совпадает (USD или близко)
            channel.currency_rate = 1.0
            logger.info(
                f"Auth-канал {channel.label}: валюта USD "
                f"(ratio={ratio:.2f})"
            )
        else:
            # Другая валюта — запоминаем курс
            channel.currency_rate = ratio
            logger.info(
                f"Auth-канал {channel.label}: валюта не-USD, "
                f"курс={ratio:.2f} "
                f"(history={history_median:.2f}, overview=${usd_price:.4f})"
            )

    # ──────────────────────────────────────────────────────
    # Расчёт медианной цены за N дней
    # ──────────────────────────────────────────────────────

    async def get_median_price(
        self,
        market_hash_name: str,
        days: int = 30,
        app_id: int = 570,
    ) -> SteamPriceData | None:
        """Текущая и медианная цена предмета на Steam Market.

        Стратегия:
        1. Всегда запрашиваем priceoverview — актуальная lowest_price (текущий листинг)
        2. Если есть auth → pricehistory для точной медианы за N дней
        3. Если нет auth → медиана из priceoverview

        Returns:
            SteamPriceData или None если предмет не найден.
        """
        # 1. Всегда получаем текущую цену (priceoverview)
        overview = await self.get_price_overview(market_hash_name, app_id=app_id)
        overview_data = self._parse_overview(overview) if overview else None

        # 2. Пробуем pricehistory для точной медианы за N дней
        history = await self.get_price_history(market_hash_name, app_id=app_id)
        history_data = self._calc_from_history(history, days) if history else None

        if overview_data and history_data:
            # Комбинируем: lowest из overview (актуальный), медиана из history (точная)
            result = SteamPriceData(
                median_price_usd=history_data.median_price_usd,
                lowest_price_usd=overview_data.lowest_price_usd,
                sales_count=history_data.sales_count,
                source="combined",
            )
            logger.debug(
                f"Steam цена [{market_hash_name}]: "
                f"lowest=${result.lowest_price_usd:.4f}, "
                f"медиана=${result.median_price_usd:.4f}, "
                f"продаж={result.sales_count} за {days}д"
            )
            return result

        if overview_data:
            logger.debug(
                f"Steam цена (overview) [{market_hash_name}]: "
                f"lowest=${overview_data.lowest_price_usd:.4f}, "
                f"медиана=${overview_data.median_price_usd:.4f}, "
                f"объём={overview_data.sales_count}/24ч"
            )
            return overview_data

        if history_data:
            logger.debug(
                f"Steam цена (history) [{market_hash_name}]: "
                f"медиана=${history_data.median_price_usd:.4f}, "
                f"продаж={history_data.sales_count} за {days}д"
            )
            return history_data

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
