"""Загрузка и горячее обновление конфигурации."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger

# Загружаем секреты из .env
load_dotenv()

# Пути
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_HISTORY_DIR = PROJECT_ROOT / "config_history"
CONFIG_HISTORY_DIR.mkdir(exist_ok=True)

# Максимум версий конфига для хранения
MAX_CONFIG_VERSIONS = 20


class EnvConfig:
    """Секреты из переменных окружения."""

    c5game_app_key: str = os.getenv("C5GAME_APP_KEY", "")
    c5game_app_secret: str = os.getenv("C5GAME_APP_SECRET", "")
    cs2dt_app_key: str = os.getenv("CS2DT_APP_KEY", "")
    cs2dt_app_secret: str = os.getenv("CS2DT_APP_SECRET", "")
    steam_api_key: str = os.getenv("STEAM_API_KEY", "")
    steam_login_secure: str = os.getenv("STEAM_LOGIN_SECURE", "").strip("'\"")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    postgres_user: str = os.getenv("POSTGRES_USER", "bot")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "secret")
    postgres_db: str = os.getenv("POSTGRES_DB", "fluxio")
    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    trade_url: str = os.getenv("TRADE_URL", "")

    http_proxy: str = os.getenv("HTTP_PROXY", "")
    https_proxy: str = os.getenv("HTTPS_PROXY", "")

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


class TradingConfig:
    """Параметры торговли из config.yaml."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.min_discount_percent: float = data.get("min_discount_percent", 15)
        self.min_price_usd: float = data.get("min_price_usd", 0.10)
        self.max_price_usd: float = data.get("max_price_usd", 5.0)
        self.max_single_purchase_usd: float = data.get("max_single_purchase_usd", 5.0)
        self.daily_limit_usd: float = data.get("daily_limit_usd", 100.0)
        self.stop_balance_usd: float = data.get("stop_balance_usd", 10.0)
        self.max_same_item_count: int = data.get("max_same_item_count", 3)
        self.min_sales_volume_7d: int = data.get("min_sales_volume_7d", 10)
        self.dry_run: bool = data.get("dry_run", True)
        self.semi_auto: bool = data.get("semi_auto", False)


class FeesConfig:
    """Комиссии торговых площадок и курс валют."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.steam_fee_percent: float = data.get("steam_fee_percent", 13.0)
        self.cs2dt_fee_percent: float = data.get("cs2dt_fee_percent", 0.0)
        self.c5game_fee_percent: float = data.get("c5game_fee_percent", 2.5)
        self.usd_to_cny_rate: float = data.get("usd_to_cny_rate", 7.25)


class MonitoringConfig:
    """Параметры мониторинга."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.interval_seconds: int = data.get("interval_seconds", 300)
        self.price_history_days: int = data.get("price_history_days", 30)
        self.new_listing_check: bool = data.get("new_listing_check", True)


class AntiManipulationConfig:
    """Параметры защиты от манипуляций."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.max_price_growth_2w_percent: float = data.get("max_price_growth_2w_percent", 30)
        self.min_sales_at_current_price: int = data.get("min_sales_at_current_price", 5)
        # Макс. отношение средней цены за 3 дня к средней за предыдущие 27 дней
        self.max_spike_ratio: float = data.get("max_spike_ratio", 2.0)
        # Макс. коэффициент вариации цены (stddev / median)
        self.max_price_cv: float = data.get("max_price_cv", 0.5)
        # Макс. отношение цены листинга к медиане 30д (защита от завышенных листингов)
        self.max_price_to_median_ratio: float = data.get("max_price_to_median_ratio", 2.0)
        # Мин. кол-во листингов на Steam Market (защита от неликвидных предметов)
        self.min_steam_listings: int = data.get("min_steam_listings", 15)


class SafetyConfig:
    """Параметры безопасности и kill switch."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.max_purchases_per_hour: int = data.get("max_purchases_per_hour", 20)
        self.balance_anomaly_percent: float = data.get("balance_anomaly_percent", 20.0)
        self.circuit_breaker_threshold: int = data.get("circuit_breaker_threshold", 5)
        self.circuit_breaker_timeout_min: int = data.get("circuit_breaker_timeout_min", 5)


class UpdateQueueConfig:
    """Параметры очереди обновления цен."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.scanner_interval_seconds: int = data.get("scanner_interval_seconds", 900)
        self.buyer_interval_seconds: int = data.get("buyer_interval_seconds", 60)
        self.freshness_candidate_minutes: int = data.get("freshness_candidate_minutes", 30)
        self.freshness_normal_hours: int = data.get("freshness_normal_hours", 2)
        self.freshness_low_hours: int = data.get("freshness_low_hours", 6)
        self.full_history_interval_hours: int = data.get("full_history_interval_hours", 24)
        # Enricher: интервал между запросами pricehistory (сек)
        self.enricher_interval_seconds: int = data.get("enricher_interval_seconds", 30)
        # Enricher: сколько дней данные считаются свежими (не переобогащать)
        self.enricher_freshness_days: int = data.get("enricher_freshness_days", 7)


class GameConfig:
    """Конфигурация одной игры."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.app_id: int = data["app_id"]
        self.name: str = data.get("name", "")
        self.enabled: bool = data.get("enabled", False)


class NotificationsConfig:
    """Параметры уведомлений."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.quiet_mode: bool = data.get("quiet_mode", False)
        self.daily_report_time: str = data.get("daily_report_time", "09:00")
        events = data.get("events", {})
        self.purchase_success: bool = events.get("purchase_success", True)
        self.purchase_error: bool = events.get("purchase_error", True)
        self.low_balance: bool = events.get("low_balance", True)
        self.daily_limit_reached: bool = events.get("daily_limit_reached", True)
        self.api_unavailable: bool = events.get("api_unavailable", True)
        self.good_deal_found: bool = events.get("good_deal_found", False)


class BlacklistConfig:
    """Чёрный список предметов."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.items: list[str] = data.get("items", [])


class WhitelistConfig:
    """Белый список предметов."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.enabled: bool = data.get("enabled", False)
        self.items: list[dict[str, Any]] = data.get("items", [])


class AppConfig:
    """Главный конфиг приложения — объединяет .env и config.yaml."""

    def __init__(self) -> None:
        self.env = EnvConfig()
        self._config_mtime: float = 0.0
        self._load_yaml()

    def _load_yaml(self) -> None:
        """Загрузить/перезагрузить config.yaml."""
        if not CONFIG_PATH.exists():
            logger.warning("config.yaml не найден, используются значения по умолчанию")
            raw: dict[str, Any] = {}
        else:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self._config_mtime = CONFIG_PATH.stat().st_mtime

        self.trading = TradingConfig(raw.get("trading", {}))
        self.fees = FeesConfig(raw.get("fees", {}))
        self.monitoring = MonitoringConfig(raw.get("monitoring", {}))
        self.anti_manipulation = AntiManipulationConfig(raw.get("anti_manipulation", {}))
        self.safety = SafetyConfig(raw.get("safety", {}))
        self.update_queue = UpdateQueueConfig(raw.get("update_queue", {}))
        self.notifications = NotificationsConfig(raw.get("notifications", {}))
        self.blacklist = BlacklistConfig(raw.get("blacklist", {}))
        self.whitelist = WhitelistConfig(raw.get("whitelist", {}))
        self.games: list[GameConfig] = [
            GameConfig(g) for g in raw.get("games", [{"app_id": 570, "name": "Dota 2", "enabled": True}])
        ]

        self._validate()

    def _validate(self) -> None:
        """Проверить корректность значений конфигурации."""
        t = self.trading
        errors: list[str] = []

        if t.min_price_usd > t.max_price_usd:
            errors.append(
                f"min_price_usd ({t.min_price_usd}) > max_price_usd ({t.max_price_usd})"
            )
        if t.min_discount_percent <= 0:
            errors.append(
                f"min_discount_percent ({t.min_discount_percent}) должен быть > 0"
            )
        if t.daily_limit_usd <= 0:
            errors.append(
                f"daily_limit_usd ({t.daily_limit_usd}) должен быть > 0"
            )
        if t.max_same_item_count < 1:
            errors.append(
                f"max_same_item_count ({t.max_same_item_count}) должен быть >= 1"
            )
        if self.monitoring.interval_seconds < 10:
            errors.append(
                f"interval_seconds ({self.monitoring.interval_seconds}) должен быть >= 10"
            )

        if errors:
            for err in errors:
                logger.error(f"Ошибка конфигурации: {err}")
            raise ValueError(f"Невалидная конфигурация: {'; '.join(errors)}")

    def reload_if_changed(self) -> bool:
        """Перечитать config.yaml если файл изменился. Возвращает True при обновлении."""
        if not CONFIG_PATH.exists():
            return False
        current_mtime = CONFIG_PATH.stat().st_mtime
        if current_mtime != self._config_mtime:
            logger.info("Обнаружено изменение config.yaml — перезагружаю конфигурацию")
            self._backup_config()
            self._load_yaml()
            return True
        return False

    def _backup_config(self) -> None:
        """Создать резервную копию текущего config.yaml."""
        if not CONFIG_PATH.exists():
            return
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = CONFIG_HISTORY_DIR / f"config_{timestamp}.yaml"
        shutil.copy2(CONFIG_PATH, backup_path)
        logger.info(f"Резервная копия конфига: {backup_path}")

        # Удалить старые версии, оставить последние MAX_CONFIG_VERSIONS
        backups = sorted(CONFIG_HISTORY_DIR.glob("config_*.yaml"))
        if len(backups) > MAX_CONFIG_VERSIONS:
            for old in backups[: len(backups) - MAX_CONFIG_VERSIONS]:
                old.unlink()
                logger.debug(f"Удалена старая версия конфига: {old}")


# Глобальный синглтон конфига
config = AppConfig()
