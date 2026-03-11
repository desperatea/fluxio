"""Тесты для конфигурации."""

from __future__ import annotations

import pytest

from fluxio.config import (
    AppConfig,
    FeesConfig,
    GameConfig,
    SafetyConfig,
    TradingConfig,
    UpdateQueueConfig,
)


def test_trading_config_defaults():
    """TradingConfig с пустым dict — значения по умолчанию."""
    tc = TradingConfig({})
    assert tc.min_discount_percent == 15
    assert tc.min_price_usd == 0.10
    assert tc.max_price_usd == 5.0
    assert tc.daily_limit_usd == 100.0
    assert tc.dry_run is True


def test_fees_config():
    """FeesConfig парсит комиссии."""
    fc = FeesConfig({"steam_fee_percent": 15.0, "cs2dt_fee_percent": 1.0})
    assert fc.steam_fee_percent == 15.0
    assert fc.cs2dt_fee_percent == 1.0
    assert fc.c5game_fee_percent == 2.5  # default


def test_safety_config():
    """SafetyConfig парсит параметры безопасности."""
    sc = SafetyConfig({"max_purchases_per_hour": 10, "balance_anomaly_percent": 30})
    assert sc.max_purchases_per_hour == 10
    assert sc.balance_anomaly_percent == 30.0
    assert sc.circuit_breaker_threshold == 5  # default


def test_update_queue_config():
    """UpdateQueueConfig парсит параметры очереди."""
    uqc = UpdateQueueConfig({"scanner_interval_seconds": 600})
    assert uqc.scanner_interval_seconds == 600
    assert uqc.buyer_interval_seconds == 60  # default


def test_game_config():
    """GameConfig парсит данные об игре."""
    gc = GameConfig({"app_id": 570, "name": "Dota 2", "enabled": True})
    assert gc.app_id == 570
    assert gc.name == "Dota 2"
    assert gc.enabled is True


def test_game_config_requires_app_id():
    """GameConfig без app_id — KeyError."""
    with pytest.raises(KeyError):
        GameConfig({})


def test_validation_min_gt_max():
    """Валидация: min_price > max_price — TradingConfig сохраняет значения, но _validate поймает."""
    tc = TradingConfig({"min_price_usd": 10.0, "max_price_usd": 1.0})
    # TradingConfig сам не валидирует — это делает AppConfig._validate()
    assert tc.min_price_usd == 10.0
    assert tc.max_price_usd == 1.0
    assert tc.min_price_usd > tc.max_price_usd


def test_trading_config_custom_values():
    """TradingConfig с кастомными значениями."""
    tc = TradingConfig({
        "min_discount_percent": 20,
        "min_price_usd": 0.50,
        "max_price_usd": 10.0,
        "daily_limit_usd": 200.0,
        "stop_balance_usd": 20.0,
        "max_same_item_count": 5,
        "dry_run": False,
        "semi_auto": True,
    })
    assert tc.min_discount_percent == 20
    assert tc.min_price_usd == 0.50
    assert tc.max_price_usd == 10.0
    assert tc.daily_limit_usd == 200.0
    assert tc.dry_run is False
    assert tc.semi_auto is True
