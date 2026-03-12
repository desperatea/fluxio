"""Точка входа Fluxio — арбитражная платформа Steam.

Тонкий lifecycle: создание контейнера, запуск воркеров, graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING

from loguru import logger

from fluxio.config import config
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.services.container import ServiceContainer
from fluxio.utils.circuit_breaker import CircuitBreaker
from fluxio.utils.logger import setup_logging
from fluxio.utils.redis_client import close_redis, get_redis

if TYPE_CHECKING:
    from fluxio.core.workers.base import BaseWorker


async def create_container() -> ServiceContainer:
    """Собрать контейнер зависимостей."""
    container = ServiceContainer()

    container.register(UnitOfWork, lambda: UnitOfWork(async_session_factory))

    container.register_instance(
        CircuitBreaker,
        CircuitBreaker(
            name="cs2dt",
            threshold=config.safety.circuit_breaker_threshold,
            timeout=config.safety.circuit_breaker_timeout_min * 60,
        ),
    )

    return container


class Bot:
    """Главный класс бота."""

    def __init__(self) -> None:
        self._running: bool = False
        self._container: ServiceContainer | None = None
        self._workers: list[BaseWorker] = []

    async def startup_checks(self) -> bool:
        """Проверки при запуске: ключи API и подключения."""
        logger.info("=" * 60)
        logger.info("Fluxio — арбитражная платформа — запуск")
        logger.info(
            f"Режим: {'DRY RUN (симуляция)' if config.trading.dry_run else 'БОЕВОЙ'}"
        )
        logger.info("=" * 60)

        if not config.env.cs2dt_app_key:
            logger.error("CS2DT_APP_KEY не задан в .env — бот не может работать")
            return False

        # Проверка Redis
        try:
            await get_redis()
        except Exception as e:
            logger.error(f"Redis недоступен: {e}")
            return False

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

    async def run(self) -> None:
        """Запуск бота."""
        setup_logging(config.env.log_level)

        if not await self.startup_checks():
            logger.error("Стартовые проверки не пройдены — бот остановлен")
            return

        self._container = await create_container()
        logger.info("ServiceContainer инициализирован")

        # API клиенты
        from fluxio.api.cs2dt_client import CS2DTClient
        from fluxio.api.steam_client import SteamClient

        cs2dt_client = CS2DTClient()
        steam_client = SteamClient()

        # Воркеры Фазы 2
        from fluxio.core.workers.scanner import ScannerWorker
        from fluxio.core.workers.updater import UpdaterWorker

        scanner = ScannerWorker(cs2dt_client)
        updater = UpdaterWorker(steam_client)
        self._workers = [scanner, updater]

        # Регистрируем воркеры для дашборда
        self._container.register_instance(ScannerWorker, scanner)
        self._container.register_instance(UpdaterWorker, updater)

        self._running = True

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig, lambda: asyncio.create_task(self.shutdown())
                )
            except NotImplementedError:
                pass

        from fluxio.dashboard.app import create_app
        dashboard = create_app(self._container)

        import uvicorn
        uvicorn_config = uvicorn.Config(
            dashboard, host="0.0.0.0", port=8080, log_level="warning"
        )
        server = uvicorn.Server(uvicorn_config)

        tasks = [
            asyncio.create_task(scanner.run(), name="scanner"),
            asyncio.create_task(updater.run(), name="updater"),
            asyncio.create_task(self.config_watcher(), name="config_watcher"),
            asyncio.create_task(server.serve(), name="dashboard"),
        ]

        logger.info(f"Запущены задачи: {[t.get_name() for t in tasks]}")

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

        for worker in self._workers:
            worker.stop()

        if self._container:
            await self._container.shutdown()

        await close_redis()
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
