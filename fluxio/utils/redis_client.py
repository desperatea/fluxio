"""Redis клиент — singleton подключение для всего процесса.

Использует redis.asyncio (входит в пакет redis>=5.0).
Ключи и структуры по DB.md раздел 7.
"""

from __future__ import annotations

import redis.asyncio as aioredis
from loguru import logger

from fluxio.config import config

# ─── Имена ключей Redis (централизованно) ───────────────────────────────────

# Очередь обновлений Steam цен (Sorted Set, score=приоритет 0-100)
KEY_UPDATE_QUEUE = "steam:update_queue"

# Время последнего обновления Steam цены (Hash: field=hash_name, value=ISO ts)
KEY_FRESHNESS = "steam:freshness"

# Кандидаты на арбитраж (Set: member=market_hash_name)
KEY_CANDIDATES = "arb:candidates"

# Кэш цены Steam (Hash: fields=median/lowest/volume, TTL 30 мин)
KEY_STEAM_PRICE = "steam:price:{}"  # .format(hash_name)
STEAM_PRICE_TTL = 30 * 60          # 30 минут в секундах

# Идемпотентность покупок (Set: member=product_id)
KEY_PURCHASED_IDS = "buy:purchased_ids"

# Pub/Sub каналы
CHANNEL_WORKER_STATUS = "channel:worker_status"
CHANNEL_PURCHASES = "channel:purchases"
CHANNEL_ALERTS = "channel:alerts"

# ─── Singleton ───────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Получить или создать singleton Redis-соединение."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            config.env.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        # Проверяем соединение при первом получении
        try:
            await _redis.ping()
            logger.info(f"Redis подключён: {config.env.redis_url}")
        except Exception as e:
            logger.error(f"Ошибка подключения к Redis: {e}")
            raise
    return _redis


async def close_redis() -> None:
    """Закрыть соединение с Redis."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info("Redis соединение закрыто")


# ─── Приоритеты очереди обновлений ──────────────────────────────────────────

class UpdatePriority:
    """Приоритеты очереди steam:update_queue (DB.md раздел 7.2)."""
    URGENT = 100    # Новый предмет, нет данных Steam
    HIGH = 80       # Был кандидатом / данные > 30 мин
    MEDIUM = 50     # В ценовом диапазоне, данные > 2ч
    LOW = 20        # Данные > 6ч
    SKIP = 0        # Пропустить (свежие данные / blacklist)
