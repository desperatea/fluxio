"""Точка входа Fluxio — арбитражная платформа Steam.

Запуск, проверка баланса и Steam аккаунта,
горячее обновление конфига, основной цикл (заглушка).
"""

from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from fluxio.api.cs2dt_client import CS2DTClient, CS2DTAPIError
from fluxio.config import config
from fluxio.utils.logger import setup_logging


class Bot:
    """Главный класс бота."""

    def __init__(self) -> None:
        self._running: bool = False
        self._paused: bool = False
        self._cs2dt_client = CS2DTClient()

    async def startup_checks(self) -> bool:
        """Проверки при запуске: баланс и Steam аккаунт.

        Returns:
            True если все проверки прошли.
        """
        logger.info("=" * 60)
        logger.info("Fluxio — арбитражная платформа — запуск")
        logger.info(f"Режим: {'DRY RUN (симуляция)' if config.trading.dry_run else 'БОЕВОЙ'}")
        logger.info("=" * 60)

        # Проверка API-ключа CS2DT
        if not config.env.cs2dt_app_key:
            logger.error("CS2DT_APP_KEY не задан в .env — бот не может работать")
            return False

        # Проверка баланса (CS2DT T-Coin, USD)
        try:
            balance_data = await self._cs2dt_client.get_balance()
            balance = float(balance_data.get("data", 0))
            user_id = balance_data.get("userId", "N/A")
            logger.info(f"Аккаунт CS2DT: userId={user_id}, баланс=${balance:.2f} USD")

            if balance < config.trading.stop_balance_usd:
                logger.warning(
                    f"Баланс ${balance:.2f} USD ниже порога остановки "
                    f"(${config.trading.stop_balance_usd:.2f} USD)"
                )
        except CS2DTAPIError as e:
            logger.error(f"Ошибка проверки баланса CS2DT: {e}")
            if not config.trading.dry_run:
                return False
            logger.warning("Dry-run режим — продолжаю работу без проверки баланса")

        # Проверка Steam аккаунта через CS2DT
        try:
            steam_data = await self._cs2dt_client.check_steam_account(
                trade_url=config.env.trade_url,
                app_id=570,
            )
            check_status = steam_data.get("checkStatus", 0)
            if check_status == 1:
                steam_info = steam_data.get("steamInfo", {})
                logger.info(
                    f"Steam аккаунт привязан: "
                    f"{steam_info.get('nickName', 'N/A')} "
                    f"(steamId={steam_info.get('steamId', 'N/A')})"
                )
            else:
                logger.warning(f"Steam аккаунт: статус проверки = {check_status}")
        except CS2DTAPIError as e:
            logger.warning(f"Не удалось проверить Steam-аккаунт: {e}")

        logger.info("Стартовые проверки завершены успешно")
        return True

    async def config_watcher(self) -> None:
        """Фоновая задача: перечитывать config.yaml каждые 60 секунд."""
        while self._running:
            try:
                if config.reload_if_changed():
                    logger.info("Конфигурация обновлена")
            except Exception as e:
                logger.error(f"Ошибка обновления конфига: {e}")
            await asyncio.sleep(60)

    async def main_loop(self) -> None:
        """Основной цикл мониторинга (заглушка для Фазы 1).

        Полная реализация — Фаза 2 (мониторинг) и Фаза 3 (покупки).
        """
        interval = config.monitoring.interval_seconds
        cycle = 0

        while self._running:
            if self._paused:
                logger.debug("Бот на паузе — пропускаю цикл")
                await asyncio.sleep(10)
                continue

            cycle += 1
            logger.info(f"--- Цикл мониторинга #{cycle} ---")

            # TODO Фаза 2: полный обход рынка Dota 2
            # TODO Фаза 2: анализ выгодности
            # TODO Фаза 3: покупка выгодных предметов
            logger.info(
                f"Цикл #{cycle} завершён (заглушка Фазы 1). "
                f"Следующий через {interval}с"
            )

            await asyncio.sleep(interval)

    async def run(self) -> None:
        """Запуск бота."""
        setup_logging(config.env.log_level)

        if not await self.startup_checks():
            logger.error("Стартовые проверки не пройдены — бот остановлен")
            await self.shutdown()
            return

        self._running = True

        # Регистрация обработчиков сигналов для graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
            except NotImplementedError:
                # Windows не поддерживает add_signal_handler
                pass

        # Запуск фоновых задач
        tasks = [
            asyncio.create_task(self.main_loop()),
            asyncio.create_task(self.config_watcher()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Задачи бота отменены")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Корректное завершение работы бота."""
        logger.info("Завершение работы бота...")
        self._running = False
        await self._cs2dt_client.close()
        logger.info("Бот остановлен")


def main() -> None:
    """Точка входа для запуска из CLI."""
    bot = Bot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Прервано пользователем (Ctrl+C)")


if __name__ == "__main__":
    main()
