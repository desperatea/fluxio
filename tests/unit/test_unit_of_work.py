"""Тесты для Unit of Work."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fluxio.db.unit_of_work import UnitOfWork
from fluxio.db.repos.item_repo import ItemRepository
from fluxio.db.repos.purchase_repo import PurchaseRepository
from fluxio.db.repos.price_repo import PriceRepository
from fluxio.db.repos.stats_repo import StatsRepository


def _make_mock_session_factory():
    """Создать mock session factory."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    factory = MagicMock(return_value=session)
    return factory, session


@pytest.mark.asyncio
async def test_uow_creates_repos():
    """UoW создаёт все 4 репозитория."""
    factory, session = _make_mock_session_factory()
    uow = UnitOfWork(factory)

    async with uow:
        assert isinstance(uow.items, ItemRepository)
        assert isinstance(uow.purchases, PurchaseRepository)
        assert isinstance(uow.prices, PriceRepository)
        assert isinstance(uow.stats, StatsRepository)


@pytest.mark.asyncio
async def test_uow_commit():
    """commit() вызывает session.commit()."""
    factory, session = _make_mock_session_factory()
    uow = UnitOfWork(factory)

    async with uow:
        await uow.commit()

    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_uow_rollback_on_exception():
    """При исключении — автоматический rollback."""
    factory, session = _make_mock_session_factory()
    uow = UnitOfWork(factory)

    with pytest.raises(ValueError):
        async with uow:
            raise ValueError("тестовая ошибка")

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_uow_close_session():
    """Сессия закрывается при выходе из контекста."""
    factory, session = _make_mock_session_factory()
    uow = UnitOfWork(factory)

    async with uow:
        pass

    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_uow_no_rollback_on_success():
    """Без исключения rollback не вызывается."""
    factory, session = _make_mock_session_factory()
    uow = UnitOfWork(factory)

    async with uow:
        await uow.commit()

    session.rollback.assert_not_awaited()
