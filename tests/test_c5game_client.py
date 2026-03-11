"""Тесты C5Game API клиента (с моками, без реальных запросов)."""

from __future__ import annotations

import pytest

from fluxio.api.c5game_client import BASE_URL, ENDPOINTS, _PATHS, C5GameAPIError, C5GameClient


def test_endpoints_defined() -> None:
    """Все верифицированные эндпоинты должны быть определены."""
    required = [
        "balance",
        "sale_search",
        "sale_create",
        "sale_modify",
        "sale_cancel",
        "price_batch",
        "order_buyer",
        "order_seller",
        "order_detail",
        "trade_quick",
        "trade_normal",
    ]
    for key in required:
        assert key in ENDPOINTS, f"Эндпоинт '{key}' не определён"


def test_base_url() -> None:
    """Base URL должен соответствовать документации."""
    assert BASE_URL == "https://openapi.c5game.com"


def test_endpoints_paths_start_with_merchant() -> None:
    """Все пути должны начинаться с /merchant/."""
    for key, (method, path) in _PATHS.items():
        assert path.startswith("/merchant/"), (
            f"Путь '{key}' ({path}) не начинается с /merchant/"
        )


def test_endpoints_methods_valid() -> None:
    """Все методы должны быть GET или POST."""
    for key, (method, path) in _PATHS.items():
        assert method in ("GET", "POST"), (
            f"Некорректный метод '{method}' для эндпоинта '{key}'"
        )


def test_api_error() -> None:
    """C5GameAPIError должен хранить метаданные."""
    error = C5GameAPIError(
        "Тестовая ошибка",
        status_code=403,
        error_code=1001,
        response_data={"success": False},
    )
    assert str(error) == "Тестовая ошибка"
    assert error.status_code == 403
    assert error.error_code == 1001
    assert error.response_data == {"success": False}


def test_client_masked_key() -> None:
    """API-ключ должен маскироваться в логах."""
    client = C5GameClient()
    masked = client._masked_key
    assert "***" in masked
    assert "test_key_12345678" != masked
