"""Обогащение с прокси-пулом: параллельные запросы через множество прокси.

Загружает прокси из proxy.txt.txt, создаёт по каналу на каждый прокси,
обогащает предметы параллельно (batch = кол-во прокси).

Запуск:
    python scripts/enrich_overnight.py
    Ctrl+C для остановки
"""

import asyncio
import signal
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
from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.db.models import SteamItem
from fluxio.utils.logger import setup_logging

# Импорты из cs2dt_first_buy
from cs2dt_first_buy import (
    APP_ID,
    MIN_STEAM_PRICE,
    step1_scan_cs2dt,
    step2_check_db,
    Candidate,
)

PROXY_FILE = Path(__file__).resolve().parent.parent / "proxy.txt.txt"
DELAY_BETWEEN_BATCHES = 8  # Секунд между пакетами запросов
PAUSE_ON_429 = 60  # Пауза при массовых 429
PAUSE_BETWEEN_ROUNDS = 300
COMMIT_EVERY = 20

_stop = False


def _handle_signal(sig: int, frame: object) -> None:
    global _stop
    logger.info(f"Получен сигнал {sig}, завершаю после текущего пакета...")
    _stop = True


def load_proxies(path: Path) -> list[str]:
    """Загрузить прокси из файла. Формат: host:port:user:pass"""
    proxies: list[str] = []
    seen: set[str] = set()
    if not path.exists():
        logger.error(f"Файл прокси не найден: {path}")
        return proxies
    for line in path.read_text().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            proxy_url = f"http://{user}:{password}@{host}:{port}"
            # Дедупликация по session ID (уникальная часть)
            if proxy_url not in seen:
                seen.add(proxy_url)
                proxies.append(proxy_url)
    return proxies


async def fetch_overview(
    session: aiohttp.ClientSession,
    proxy: str,
    hash_name: str,
    app_id: int = 570,
) -> dict | None:
    """Один запрос priceoverview через конкретный прокси."""
    url = (
        f"https://steamcommunity.com/market/priceoverview/"
        f"?appid={app_id}&currency=1"
        f"&market_hash_name={url_quote(hash_name, safe='')}"
    )
    try:
        async with session.get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 429:
                return {"_429": True}
            if resp.status >= 400:
                return None
            ct = resp.content_type or ""
            if "json" not in ct and "javascript" not in ct:
                return None
            data = await resp.json(content_type=None)
            if data and data.get("success"):
                return data
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception):
        return None


def parse_price(s: str) -> float | None:
    """Парсит строку цены Steam типа '$0.03' или '$1,234.56'."""
    if not s:
        return None
    clean = s.replace("$", "").replace(",", "").strip()
    try:
        return float(clean)
    except (ValueError, TypeError):
        return None


async def enrich_batch(
    http_session: aiohttp.ClientSession,
    proxies: list[str],
    candidates: list[Candidate],
    db_session: AsyncSession,
    now: str,
) -> tuple[int, int, int]:
    """Обогатить пакет предметов параллельно через разные прокси.

    Returns: (enriched, failed, rate_limited)
    """
    tasks = []
    for c, proxy in zip(candidates, proxies):
        tasks.append(fetch_overview(http_session, proxy, c.hash_name, APP_ID))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    enriched = 0
    failed = 0
    rate_limited = 0

    for c, result in zip(candidates, results):
        if isinstance(result, Exception):
            failed += 1
            continue

        if result is None:
            failed += 1
            continue

        if result.get("_429"):
            rate_limited += 1
            continue

        median = parse_price(result.get("median_price", ""))
        lowest = parse_price(result.get("lowest_price", ""))
        volume_str = result.get("volume", "0")
        volume = int(volume_str.replace(",", "")) if volume_str else 0

        c.median_price = median
        c.lowest_price = lowest
        c.volume_24h = volume

        if median and median > 0:
            c.steam_price = median
        elif lowest and lowest > 0:
            c.steam_price = lowest

        if c.steam_price > 0:
            steam_after_fee = c.steam_price * 0.85
            c.profit = round(steam_after_fee - c.cs2dt_price, 4)
            c.profit_pct = round(
                (c.profit / c.cs2dt_price) * 100, 2
            ) if c.cs2dt_price > 0 else 0

        # Сохраняем в БД через ORM
        existing = (await db_session.execute(
            select(SteamItem).where(SteamItem.hash_name == c.hash_name)
        )).scalar_one_or_none()

        if existing:
            if c.steam_price:
                existing.sell_price_usd = c.steam_price
            existing.median_price_usd = c.median_price
            existing.lowest_price_usd = c.lowest_price
            existing.volume_24h = c.volume_24h
            existing.updated_at = now
        else:
            new_item = SteamItem(
                hash_name=c.hash_name,
                name=c.hash_name,
                sell_price_usd=c.steam_price or 0,
                sell_listings=0,
                median_price_usd=c.median_price,
                lowest_price_usd=c.lowest_price,
                volume_24h=c.volume_24h,
                first_seen_at=now,
                updated_at=now,
            )
            db_session.add(new_item)
        enriched += 1

    return enriched, failed, rate_limited


async def main() -> None:
    global _stop

    setup_logging("INFO")
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    await init_db()

    # Загрузка прокси
    proxies = load_proxies(PROXY_FILE)
    if not proxies:
        logger.error("Нет прокси! Положите прокси в proxy.txt.txt")
        return

    batch_size = len(proxies)
    logger.info("=" * 70)
    logger.info("  ОБОГАЩЕНИЕ С ПРОКСИ-ПУЛОМ")
    logger.info(f"  Прокси: {batch_size} шт (параллельных каналов)")
    logger.info(f"  Задержка между пакетами: {DELAY_BETWEEN_BATCHES}с")
    logger.info(f"  Скорость: ~{batch_size * 60 // DELAY_BETWEEN_BATCHES} предм/мин")
    logger.info(f"  Ctrl+C для остановки")
    logger.info("=" * 70)

    cs2dt = CS2DTClient()
    http_session = aiohttp.ClientSession(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    round_num = 0
    total_enriched_all = 0

    try:
        while not _stop:
            round_num += 1
            logger.info(f"\n{'─' * 70}")
            logger.info(f"  РАУНД {round_num} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'─' * 70}")

            # Шаг 1: Сканируем CS2DT
            try:
                cs2dt_items = await step1_scan_cs2dt(cs2dt)
            except Exception as e:
                logger.error(f"Ошибка сканирования CS2DT: {e}")
                await asyncio.sleep(PAUSE_BETWEEN_ROUNDS)
                continue

            if not cs2dt_items:
                logger.warning("Нет предметов на CS2DT, ждём...")
                await asyncio.sleep(PAUSE_BETWEEN_ROUNDS)
                continue

            async with async_session() as db_session:
                # Шаг 2: Проверяем в БД
                with_steam, without_steam = await step2_check_db(db_session, cs2dt_items)

                if not without_steam:
                    logger.info("Все предметы уже обогащены!")
                    await asyncio.sleep(PAUSE_BETWEEN_ROUNDS)
                    continue

                total = len(without_steam)
                logger.info(
                    f"Нужно обогатить: {total} предметов "
                    f"(уже есть данные: {len(with_steam)})"
                )

                # Шаг 3: Параллельное обогащение пакетами
                now = datetime.now(timezone.utc).isoformat()
                enriched_total = 0
                failed_total = 0
                rate_limited_total = 0
                start_time = datetime.now(timezone.utc)
                proxy_idx = 0

                for batch_start in range(0, total, batch_size):
                    if _stop:
                        logger.info("Остановка по сигналу...")
                        break

                    batch_end = min(batch_start + batch_size, total)
                    batch = without_steam[batch_start:batch_end]
                    actual_batch_size = len(batch)

                    # Ротация прокси для каждого пакета
                    batch_proxies = []
                    for j in range(actual_batch_size):
                        batch_proxies.append(proxies[(proxy_idx + j) % len(proxies)])
                    proxy_idx = (proxy_idx + actual_batch_size) % len(proxies)

                    enriched, failed, rate_limited = await enrich_batch(
                        http_session, batch_proxies, batch, db_session, now,
                    )

                    enriched_total += enriched
                    failed_total += failed
                    rate_limited_total += rate_limited
                    total_enriched_all += enriched

                    # Коммит
                    if (batch_start // batch_size) % 2 == 0:
                        await db_session.commit()

                    # Прогресс
                    processed = batch_end
                    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                    rate_pm = enriched_total / max(elapsed, 1) * 60
                    remaining = total - processed
                    eta_min = remaining / max(rate_pm, 0.01)

                    # Логируем прибыльные из этого батча
                    for c in batch:
                        if c.steam_price > 0 and c.profit_pct >= 45.0:
                            logger.info(
                                f"  ★ {c.profit_pct:>6.1f}%  "
                                f"CS2DT ${c.cs2dt_price:.2f} → Steam ${c.steam_price:.2f}  "
                                f"vol={c.volume_24h}  {c.hash_name[:50]}"
                            )

                    logger.info(
                        f"  [{processed}/{total}] "
                        f"+{enriched} ok, {failed} fail, {rate_limited} 429  "
                        f"| всего: {enriched_total} | "
                        f"~{rate_pm:.0f}/мин, ETA ~{eta_min:.0f} мин"
                    )

                    # Если много 429 — увеличить паузу
                    if rate_limited > actual_batch_size * 0.5:
                        logger.warning(
                            f"Много 429 ({rate_limited}/{actual_batch_size}), "
                            f"пауза {PAUSE_ON_429}с..."
                        )
                        await asyncio.sleep(PAUSE_ON_429)
                    else:
                        await asyncio.sleep(DELAY_BETWEEN_BATCHES)

                await db_session.commit()

                # Итоги раунда
                elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                logger.info(f"\n  Раунд {round_num} за {elapsed/60:.1f} мин:")
                logger.info(f"    Обогащено: {enriched_total}")
                logger.info(f"    Ошибок: {failed_total}")
                logger.info(f"    Rate limited: {rate_limited_total}")
                logger.info(f"    Всего за сессию: {total_enriched_all}")

                # Топ прибыльных
                profitable = [
                    c for c in without_steam
                    if c.steam_price > 0 and c.profit_pct >= 45.0
                ]
                if profitable:
                    profitable.sort(key=lambda c: c.profit_pct, reverse=True)
                    logger.info(f"\n  Найдено прибыльных (>45%): {len(profitable)}")
                    for p in profitable[:15]:
                        logger.info(
                            f"    {p.profit_pct:>6.1f}%  "
                            f"CS2DT ${p.cs2dt_price:.2f} → Steam ${p.steam_price:.2f}  "
                            f"vol={p.volume_24h}  {p.hash_name[:50]}"
                        )

            if _stop:
                break

            if failed_total > 0 or rate_limited_total > 0:
                logger.info(f"Пауза {PAUSE_BETWEEN_ROUNDS}с перед следующим раундом...")
                await asyncio.sleep(PAUSE_BETWEEN_ROUNDS)
            else:
                logger.info("Все обогащены! Пауза перед пересканированием...")
                await asyncio.sleep(PAUSE_BETWEEN_ROUNDS)

    except KeyboardInterrupt:
        logger.info("Прервано пользователем (Ctrl+C)")
    finally:
        await http_session.close()
        await cs2dt.close()

    logger.info("=" * 70)
    logger.info(f"  ОБОГАЩЕНИЕ ЗАВЕРШЕНО")
    logger.info(f"  Раундов: {round_num}, обогащено: {total_enriched_all}")
    logger.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
