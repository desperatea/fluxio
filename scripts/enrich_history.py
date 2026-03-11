"""Обогащение pricehistory через второй Steam-аккаунт + второй прокси.

Работает параллельно с enrich_light.py (priceoverview).
Использует STEAM_LOGIN_SECURE2 + HTTP_PROXY2 из .env.

Запуск:
    python scripts/enrich_history.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as url_quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import aiohttp
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_helper import async_session, init_db
from fluxio.db.models import SteamItem, SteamSalesHistory
from fluxio.utils.logger import setup_logging
from fluxio.utils.rate_limiter import RetryConfig, TokenBucketRateLimiter

APP_ID = 570

RETRY = RetryConfig(max_retries=3, base_delay=5.0, max_delay=60.0, backoff_factor=3.0)


class SteamHistoryClient:
    """Минимальный клиент для pricehistory через второй аккаунт + прокси."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = TokenBucketRateLimiter(rate=0.33, burst=1, name="steam2")
        self._semaphore = asyncio.Semaphore(1)

        # Cookies второго аккаунта
        login_secure = os.getenv("STEAM_LOGIN_SECURE2", "").strip("'\"")
        session_id = os.getenv("SESSION_ID2", "").strip("'\"")
        browser_id = os.getenv("BROUSER_ID2", "").strip("'\"")

        self._cookies: dict[str, str] = {}
        if login_secure:
            self._cookies["steamLoginSecure"] = login_secure
        if session_id:
            self._cookies["sessionid"] = session_id
        if browser_id:
            self._cookies["browserid"] = browser_id

        if not login_secure:
            logger.error("STEAM_LOGIN_SECURE2 не задан!")
            sys.exit(1)

        # Второй прокси
        self._proxy_url: str | None = None
        raw_proxy = os.getenv("HTTP_PROXY2", "").strip("'\"")
        if raw_proxy:
            parts = raw_proxy.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                self._proxy_url = f"http://{user}:{password}@{host}:{port}"
            elif len(parts) == 2:
                self._proxy_url = f"http://{raw_proxy}"
            elif raw_proxy.startswith(("http://", "https://")):
                self._proxy_url = raw_proxy

        if self._proxy_url:
            logger.info(f"Steam2 прокси: {parts[0]}:{parts[1] if len(parts) > 1 else '?'}")
        else:
            logger.warning("HTTP_PROXY2 не задан — pricehistory пойдёт напрямую")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/132.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                cookies=self._cookies,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_price_history(
        self, market_hash_name: str, app_id: int = 570, currency: int = 1
    ) -> list[list] | None:
        """Получить историю цен через второй аккаунт + прокси."""
        url = (
            f"https://steamcommunity.com/market/pricehistory/"
            f"?appid={app_id}&currency={currency}"
            f"&market_hash_name={url_quote(market_hash_name, safe='')}"
        )
        session = await self._get_session()

        for attempt in range(RETRY.max_retries + 1):
            retry_delay: float | None = None

            async with self._semaphore:
                await self._rate_limiter.acquire()
                try:
                    async with session.get(url, proxy=self._proxy_url) as resp:
                        if resp.status == 429:
                            delay = RETRY.get_delay(attempt)
                            logger.warning(
                                f"Steam2 429 — ожидание {delay:.0f}с (попытка {attempt + 1})"
                            )
                            if attempt < RETRY.max_retries:
                                retry_delay = delay
                            else:
                                return None
                        elif resp.status >= 400:
                            logger.error(f"Steam2 HTTP {resp.status}: {market_hash_name[:50]}")
                            return None
                        else:
                            ct = resp.content_type or ""
                            if "json" not in ct and "javascript" not in ct:
                                logger.warning(f"Steam2 не-JSON ({ct})")
                                return None
                            data = await resp.json(content_type=None)
                            if data and data.get("success") and data.get("prices"):
                                return data["prices"]
                            return None
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    delay = RETRY.get_delay(attempt)
                    logger.error(f"Steam2 ошибка: {e} — retry {delay:.0f}с")
                    if attempt < RETRY.max_retries:
                        retry_delay = delay
                    else:
                        return None

            if retry_delay is not None:
                await asyncio.sleep(retry_delay)

        return None


async def main() -> None:
    setup_logging("INFO")
    logger.info("=" * 60)
    logger.info("  ОБОГАЩЕНИЕ PRICEHISTORY (второй аккаунт + прокси)")
    logger.info("=" * 60)

    await init_db()

    steam2 = SteamHistoryClient()

    # Тест подключения
    logger.info("Тест прокси...")
    test = await steam2.get_price_history("Inscribed Paraflare Cannon", app_id=570)
    if test:
        logger.info(f"Тест OK — {len(test)} записей в истории")
    else:
        logger.error("Тест FAILED — прокси или cookies не работают")
        await steam2.close()
        return

    try:
        async with async_session() as session:
            # Находим предметы без истории, но с priceoverview
            subq = select(SteamSalesHistory.hash_name).distinct()
            result = await session.execute(
                select(SteamItem.hash_name)
                .where(SteamItem.volume_24h.isnot(None))
                .where(SteamItem.volume_24h > 0)
                .where(SteamItem.hash_name.notin_(subq))
                .order_by(SteamItem.volume_24h.desc())
            )
            items = [row[0] for row in result.all()]

            if not items:
                logger.info("Все предметы уже имеют историю продаж")
                return

            logger.info(f"Нужно загрузить историю для {len(items)} предметов")

            enriched = 0
            failed = 0
            now = datetime.now(timezone.utc).isoformat()

            for i, hash_name in enumerate(items, 1):
                history = await steam2.get_price_history(hash_name, app_id=APP_ID)

                if history:
                    rows_inserted = 0
                    for entry in history:
                        if len(entry) < 3:
                            continue
                        date_str, price, volume_str = entry[0], entry[1], entry[2]
                        try:
                            clean = date_str.split(": +")[0].strip()
                            dt = datetime.strptime(clean, "%b %d %Y %H")
                            sale_date = dt.strftime("%Y-%m-%d %H:00:00")
                        except (ValueError, IndexError):
                            continue
                        vol = (
                            int(volume_str.replace(",", ""))
                            if isinstance(volume_str, str)
                            else int(volume_str)
                        )
                        stmt = pg_insert(SteamSalesHistory).values(
                            hash_name=hash_name,
                            sale_date=sale_date,
                            price_usd=float(price),
                            volume=vol,
                        ).on_conflict_do_nothing(
                            constraint="uq_steam_sales_hash_date",
                        )
                        await session.execute(stmt)
                        rows_inserted += 1
                    if rows_inserted > 0:
                        enriched += 1
                else:
                    failed += 1

                # Коммит и прогресс каждые 10 предметов
                if i % 10 == 0:
                    await session.commit()
                    pct = i * 100 // len(items)
                    elapsed = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(now)
                    ).total_seconds()
                    rate = i / max(elapsed, 1) * 60
                    eta = (len(items) - i) / max(rate, 0.01)
                    logger.info(
                        f"  Прогресс: {i}/{len(items)} ({pct}%), "
                        f"обогащено: {enriched}, ошибок: {failed}, "
                        f"~{rate:.1f} предм/мин, ETA ~{eta:.0f} мин"
                    )

            await session.commit()
            logger.info(
                f"Готово: обогащено {enriched}, ошибок {failed}, "
                f"всего {len(items)}"
            )

    finally:
        await steam2.close()

    logger.info("Обогащение pricehistory завершено")


if __name__ == "__main__":
    asyncio.run(main())
