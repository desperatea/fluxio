"""Риск-менеджмент и защитные лимиты.

Проверки перед каждой покупкой (SPEC.md раздел 12).
Все цены и лимиты — в USD (CS2DT — основная площадка).
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from fluxio.api.cs2dt_client import CS2DTClient
from fluxio.config import config
from fluxio.db.repository import get_same_item_count_24h, get_today_spent, is_product_purchased


class SafetyCheck:
    """Результат проверки безопасности."""

    def __init__(self, passed: bool, reason: str = "") -> None:
        self.passed = passed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.passed


async def check_balance(
    cs2dt_client: CS2DTClient,
    item_price: float,
) -> SafetyCheck:
    """Проверить достаточность баланса (USD).

    - Баланс >= цена предмета
    - Баланс >= stop_balance_usd
    """
    try:
        data = await cs2dt_client.get_balance()
        balance = float(data.get("data", 0))
    except Exception as e:
        logger.error(f"Ошибка получения баланса: {e}")
        return SafetyCheck(False, f"Не удалось получить баланс: {e}")

    if balance < config.trading.stop_balance_usd:
        msg = (
            f"Баланс ${balance:.2f} ниже порога остановки "
            f"(${config.trading.stop_balance_usd:.2f})"
        )
        logger.warning(msg)
        return SafetyCheck(False, msg)

    if balance < item_price:
        msg = f"Недостаточно средств: баланс ${balance:.2f}, цена ${item_price:.2f}"
        logger.info(msg)
        return SafetyCheck(False, msg)

    return SafetyCheck(True)


async def check_daily_limit(session: AsyncSession, item_price: float) -> SafetyCheck:
    """Проверить дневной лимит трат (USD)."""
    daily_limit = config.trading.daily_limit_usd
    spent_today = await get_today_spent(session)
    remaining = daily_limit - spent_today

    if spent_today + item_price > daily_limit:
        msg = (
            f"Дневной лимит: потрачено ${spent_today:.2f} из "
            f"${daily_limit:.2f}, осталось ${remaining:.2f}"
        )
        logger.warning(msg)
        return SafetyCheck(False, msg)

    return SafetyCheck(True)


async def check_same_item_limit(
    session: AsyncSession,
    market_hash_name: str,
) -> SafetyCheck:
    """Проверить лимит одинаковых предметов за 24 часа."""
    count = await get_same_item_count_24h(session, market_hash_name)
    if count >= config.trading.max_same_item_count:
        msg = (
            f"Лимит одинаковых предметов: {market_hash_name} "
            f"уже куплено {count}/{config.trading.max_same_item_count} за 24ч"
        )
        logger.info(msg)
        return SafetyCheck(False, msg)

    return SafetyCheck(True)


async def check_idempotency(
    session: AsyncSession,
    product_id: str,
) -> SafetyCheck:
    """Проверить, не был ли product_id уже куплен (идемпотентность)."""
    if await is_product_purchased(session, product_id):
        return SafetyCheck(False, f"product_id {product_id} уже куплен ранее")
    return SafetyCheck(True)


async def check_price_range(price_usd: float) -> SafetyCheck:
    """Проверить что цена в допустимом диапазоне (USD)."""
    if not (config.trading.min_price_usd <= price_usd <= config.trading.max_price_usd):
        msg = (
            f"Цена ${price_usd:.2f} вне диапазона "
            f"[${config.trading.min_price_usd}, ${config.trading.max_price_usd}]"
        )
        return SafetyCheck(False, msg)
    return SafetyCheck(True)


async def run_all_checks(
    cs2dt_client: CS2DTClient,
    session: AsyncSession,
    product_id: str,
    market_hash_name: str,
    price_usd: float,
) -> SafetyCheck:
    """Выполнить все проверки безопасности перед покупкой.

    Returns:
        SafetyCheck с результатом. Если не прошла — reason содержит причину.
    """
    checks = [
        ("Идемпотентность", await check_idempotency(session, product_id)),
        ("Диапазон цены", await check_price_range(price_usd)),
        ("Лимит одинаковых", await check_same_item_limit(session, market_hash_name)),
        ("Дневной лимит", await check_daily_limit(session, price_usd)),
        ("Баланс", await check_balance(cs2dt_client, price_usd)),
    ]

    for name, check in checks:
        if not check:
            logger.debug(f"Проверка '{name}' не пройдена: {check.reason}")
            return check

    return SafetyCheck(True)
