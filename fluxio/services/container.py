"""ServiceContainer — DI контейнер с управлением жизненным циклом."""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from loguru import logger

T = TypeVar("T")


class ServiceContainer:
    """Контейнер зависимостей с управлением жизненным циклом.

    Принципы:
    - Ленивая инициализация: сервис создаётся при первом запросе
    - Singleton по умолчанию: один экземпляр на весь процесс
    - Graceful shutdown: закрытие в обратном порядке регистрации
    - Type-safe: регистрация и получение по типу
    """

    def __init__(self) -> None:
        self._factories: dict[type, Any] = {}
        self._instances: dict[type, Any] = {}
        self._init_order: list[type] = []

    def register(self, interface: type[T], factory: Any) -> None:
        """Зарегистрировать фабрику для интерфейса."""
        self._factories[interface] = factory
        logger.debug(f"Зарегистрирован сервис: {interface.__name__}")

    def register_instance(self, interface: type[T], instance: T) -> None:
        """Зарегистрировать готовый экземпляр (без фабрики)."""
        self._instances[interface] = instance
        self._init_order.append(interface)
        logger.debug(f"Зарегистрирован экземпляр: {interface.__name__}")

    async def get(self, interface: type[T]) -> T:
        """Получить экземпляр сервиса (singleton).

        Raises:
            KeyError: Сервис не зарегистрирован.
        """
        if interface in self._instances:
            return self._instances[interface]

        if interface not in self._factories:
            raise KeyError(
                f"Сервис {interface.__name__} не зарегистрирован. "
                f"Доступные: {[t.__name__ for t in self._factories]}"
            )

        factory = self._factories[interface]
        instance = factory()

        if asyncio.iscoroutine(instance):
            instance = await instance

        self._instances[interface] = instance
        self._init_order.append(interface)
        logger.info(f"Создан сервис: {interface.__name__}")
        return instance

    async def shutdown(self) -> None:
        """Graceful shutdown: закрытие в обратном порядке."""
        for iface in reversed(self._init_order):
            instance = self._instances.get(iface)
            if instance and hasattr(instance, "close"):
                try:
                    result = instance.close()
                    if asyncio.iscoroutine(result):
                        await result
                    logger.info(f"Закрыт сервис: {iface.__name__}")
                except Exception as e:
                    logger.error(f"Ошибка при закрытии {iface.__name__}: {e}")

        self._instances.clear()
        self._init_order.clear()

    def is_registered(self, interface: type) -> bool:
        """Проверить, зарегистрирован ли сервис."""
        return interface in self._factories or interface in self._instances

    def is_initialized(self, interface: type) -> bool:
        """Проверить, создан ли экземпляр сервиса."""
        return interface in self._instances
