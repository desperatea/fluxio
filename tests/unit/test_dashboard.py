"""Тесты для FastAPI дашборда."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fluxio.dashboard.app import create_app
from fluxio.services.container import ServiceContainer
from fluxio.utils.circuit_breaker import CircuitBreaker


@pytest.fixture
def client():
    """Тестовый HTTP клиент."""
    container = ServiceContainer()
    container.register_instance(
        CircuitBreaker,
        CircuitBreaker(name="test_api", threshold=5, timeout=300),
    )
    app = create_app(container)
    return TestClient(app)


def test_health(client):
    """GET /health возвращает 200 OK."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data


def test_api_status(client):
    """GET /api/v1/status возвращает JSON со статусом."""
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert "config" in data
    assert "circuit_breaker" in data


def test_api_workers(client):
    """GET /api/v1/workers возвращает список воркеров."""
    resp = client.get("/api/v1/workers")
    assert resp.status_code == 200
    data = resp.json()
    names = {w["name"] for w in data["workers"]}
    assert "scanner" in names
    assert "updater" in names


def test_admin_page(client):
    """GET /admin/ возвращает HTML страницу."""
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert "Fluxio" in resp.text
    assert "Circuit Breaker" in resp.text


def test_admin_logs_page(client):
    """GET /admin/logs возвращает HTML с SSE клиентом."""
    resp = client.get("/admin/logs")
    assert resp.status_code == 200
    assert "EventSource" in resp.text
    assert "/sse/logs" in resp.text
