"""Тесты для ServiceContainer."""

from __future__ import annotations

import asyncio

import pytest

from fluxio.services.container import ServiceContainer


class DummyService:
    """Тестовый сервис."""

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class AnotherService:
    """Ещё один тестовый сервис."""
    pass


@pytest.mark.asyncio
async def test_register_and_get():
    """Регистрация и получение сервиса."""
    container = ServiceContainer()
    container.register(DummyService, lambda: DummyService("test"))

    service = await container.get(DummyService)
    assert isinstance(service, DummyService)
    assert service.name == "test"


@pytest.mark.asyncio
async def test_singleton():
    """Повторное получение возвращает тот же экземпляр."""
    container = ServiceContainer()
    container.register(DummyService, lambda: DummyService())

    s1 = await container.get(DummyService)
    s2 = await container.get(DummyService)
    assert s1 is s2


@pytest.mark.asyncio
async def test_register_instance():
    """Регистрация готового экземпляра."""
    container = ServiceContainer()
    instance = DummyService("pre-created")
    container.register_instance(DummyService, instance)

    result = await container.get(DummyService)
    assert result is instance
    assert result.name == "pre-created"


@pytest.mark.asyncio
async def test_get_unregistered_raises():
    """Запрос незарегистрированного сервиса — KeyError."""
    container = ServiceContainer()
    with pytest.raises(KeyError, match="DummyService"):
        await container.get(DummyService)


@pytest.mark.asyncio
async def test_shutdown_calls_close():
    """shutdown() вызывает close() у всех сервисов."""
    container = ServiceContainer()
    container.register(DummyService, lambda: DummyService())

    service = await container.get(DummyService)
    assert not service.closed

    await container.shutdown()
    assert service.closed


@pytest.mark.asyncio
async def test_shutdown_reverse_order():
    """shutdown() закрывает в обратном порядке инициализации."""
    container = ServiceContainer()
    close_order: list[str] = []

    class ServiceA:
        async def close(self) -> None:
            close_order.append("A")

    class ServiceB:
        async def close(self) -> None:
            close_order.append("B")

    container.register(ServiceA, ServiceA)
    container.register(ServiceB, ServiceB)

    await container.get(ServiceA)  # Инициализирован первым
    await container.get(ServiceB)  # Инициализирован вторым

    await container.shutdown()
    assert close_order == ["B", "A"]


@pytest.mark.asyncio
async def test_is_registered():
    """is_registered() проверяет наличие регистрации."""
    container = ServiceContainer()
    assert not container.is_registered(DummyService)

    container.register(DummyService, lambda: DummyService())
    assert container.is_registered(DummyService)


@pytest.mark.asyncio
async def test_is_initialized():
    """is_initialized() проверяет наличие экземпляра."""
    container = ServiceContainer()
    container.register(DummyService, lambda: DummyService())

    assert not container.is_initialized(DummyService)
    await container.get(DummyService)
    assert container.is_initialized(DummyService)


@pytest.mark.asyncio
async def test_async_factory():
    """Поддержка async фабрик."""
    container = ServiceContainer()

    async def create_service():
        return DummyService("async")

    container.register(DummyService, create_service)
    service = await container.get(DummyService)
    assert service.name == "async"
