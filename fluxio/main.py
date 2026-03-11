"""Точка входа Fluxio — арбитражная платформа Steam.

Тонкий lifecycle: создание контейнера, запуск воркеров, graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal

from loguru import logger

from fluxio.config import config
from fluxio.db.session import async_session_factory
from fluxio.db.unit_of_work import UnitOfWork
from fluxio.services.container import ServiceContainer
from fluxio.utils.circuit_breaker import CircuitBreaker
from fluxio.utils.logger import setup_logging


async def create_container() -> ServiceContainer:
    """Собрать контейнер зависимостей."""
    container = ServiceContainer()

    # Unit of Work — фабрика, каждый раз новый экземпляр
    container.register(UnitOfWork, lambda: UnitOfWork(async_session_factory))

    # Circuit Breaker для внешних API
    container.register_instance(
        CircuitBreaker,
        CircuitBreaker(
            name="cs2dt",
            threshold=config.safety.circuit_breaker_threshold,
            timeout=config.safety.circuit_breaker_timeout_min * 60,
        ),
    )

    # TODO Фаза 2+: MarketClient, PriceProvider, Strategy, Notifier

    return container


class Bot:
    """Главный класс бота."""

    def __init__(self) -> None:
        self._running: bool = False
        self._paused: bool = False
        self._container: ServiceContainer | None = None

    async def startup_checks(self) -> bool:
        """Проверки при запуске: баланс и Steam аккаунт."""
        logger.info("=" * 60)
        logger.info("Fluxio — арбитражная платформа — запуск")
        logger.info(f"Режим: {'DRY RUN (симуляция)' if config.trading.dry_run else 'БОЕВОЙ'}")
        logger.info("=" * 60)

        if not config.env.cs2dt_app_key:
            logger.error("CS2DT_APP_KEY не задан в .env — бот не может работать")
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

    async def main_loop(self) -> None:
        """Основной цикл мониторинга (заглушка для Фазы 2)."""
        interval = config.monitoring.interval_seconds
        cycle = 0

        while self._running:
            if self._paused:
                logger.debug("Бот на паузе — пропускаю цикл")
                await asyncio.sleep(10)
                continue

            cycle += 1
            logger.info(f"--- Цикл мониторинга #{cycle} ---")

            # TODO Фаза 2: полный обход рынка
            # TODO Фаза 3: покупка выгодных предметов
            logger.info(
                f"Цикл #{cycle} завершён (заглушка). "
                f"Следующий через {interval}с"
            )

            await asyncio.sleep(interval)

    async def run(self) -> None:
        """Запуск бота."""
        setup_logging(config.env.log_level)

        if not await self.startup_checks():
            logger.error("Стартовые проверки не пройдены — бот остановлен")
            return

        # Создание DI-контейнера
        self._container = await create_container()
        logger.info("ServiceContainer инициализирован")

        self._running = True

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
            except NotImplementedError:
                pass

        # Запуск дашборда в фоне
        from fluxio.dashboard.app import create_app
        dashboard = create_app(self._container)

        import uvicorn
        uvicorn_config = uvicorn.Config(
            dashboard, host="0.0.0.0", port=8080, log_level="warning"
        )
        server = uvicorn.Server(uvicorn_config)

        tasks = [
            asyncio.create_task(self.main_loop()),
            asyncio.create_task(self.config_watcher()),
            asyncio.create_task(server.serve()),
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
        if self._container:
            await self._container.shutdown()
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
