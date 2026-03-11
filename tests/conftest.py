"""Общие фикстуры для тестов."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Generator

import pytest
import yaml


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Установить тестовое окружение — dry_run всегда True."""
    monkeypatch.setenv("C5GAME_APP_KEY", "test_key_12345678")
    monkeypatch.setenv("C5GAME_APP_SECRET", "test_secret_12345678")
    monkeypatch.setenv("STEAM_API_KEY", "test_steam_key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_USER", "test")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    monkeypatch.setenv("POSTGRES_DB", "test_db")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")


@pytest.fixture
def test_config_yaml(tmp_path: Path) -> Path:
    """Создать тестовый config.yaml с dry_run=True."""
    config_data: dict[str, Any] = {
        "trading": {
            "min_discount_percent": 15,
            "min_price_cny": 0.29,
            "max_price_cny": 5.0,
            "max_single_purchase_cny": 5.0,
            "daily_limit_cny": 500.0,
            "stop_balance_cny": 50.0,
            "max_same_item_count": 3,
            "min_sales_volume_7d": 10,
            "usd_to_cny_rate": 7.25,
            "dry_run": True,
            "semi_auto": False,
        },
        "monitoring": {
            "interval_seconds": 300,
            "price_history_days": 30,
            "new_listing_check": True,
        },
        "anti_manipulation": {
            "max_price_growth_2w_percent": 30,
            "min_sales_at_current_price": 5,
        },
        "notifications": {
            "quiet_mode": False,
            "daily_report_time": "09:00",
            "events": {
                "purchase_success": True,
                "purchase_error": True,
                "low_balance": True,
                "daily_limit_reached": True,
                "api_unavailable": True,
                "good_deal_found": False,
            },
        },
        "blacklist": {"items": []},
        "whitelist": {"enabled": False, "items": []},
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config_data, f)
    return config_path
