"""SQLAlchemy модели базы данных.

Схема таблиц по SPEC.md раздел 8.1.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""
    pass


class PriceHistory(Base):
    """История цен — основная таблица, много данных."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)  # 'c5game' | 'steam'
    price: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(5), nullable=False)  # 'CNY' | 'USD'
    sales_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_price_history_name_time", "market_hash_name", recorded_at.desc()),
    )


class Purchase(Base):
    """Совершённые покупки."""

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    price_cny: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    steam_price_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    discount_percent: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # pending|success|failed|cancelled|unknown
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    api_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    purchased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ActiveOrder(Base):
    """Активные заказы для отслеживания."""

    __tablename__ = "active_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    purchase_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Watchlist(Base):
    """Watchlist — hashName под усиленным мониторингом."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    max_price_cny: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Blacklist(Base):
    """Чёрный список предметов."""

    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    steam_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApiLog(Base):
    """Сырые ответы API для отладки."""

    __tablename__ = "api_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConfigVersion(Base):
    """Версии конфигурации."""

    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class DailyStat(Base):
    """Ежедневная агрегированная статистика."""

    __tablename__ = "daily_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    items_purchased: Mapped[int] = mapped_column(Integer, default=0)
    total_spent_cny: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    potential_value_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    potential_profit_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)


class Item(Base):
    """Уникальный предмет Dota 2 (каталог по market_hash_name)."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    item_name: Mapped[str] = mapped_column(String(500), nullable=False)
    app_id: Mapped[int] = mapped_column(Integer, nullable=False, default=570)
    cs2dt_item_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    hero: Mapped[str | None] = mapped_column(String(100), nullable=True)
    slot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rarity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Цена на CS2DT (USD)
    price_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    auto_deliver_price_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    auto_deliver_quantity: Mapped[int] = mapped_column(Integer, default=0)
    # Тип покупки: "normal" | "quick" | "both"
    buy_type: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    # Старые поля (совместимость)
    min_price_cny: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    listings_count: Mapped[int] = mapped_column(Integer, default=0)
    steam_price_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    steam_volume_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_items_min_price", "min_price_cny"),
        Index("idx_items_price_usd", "price_usd"),
        Index("idx_items_buy_type", "buy_type"),
    )


class SaleListing(Base):
    """Ордер на продажу на C5Game, подходящий под критерии по цене."""

    __tablename__ = "sale_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    c5_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    item_name: Mapped[str] = mapped_column(String(500), nullable=False)
    price_cny: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    seller_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery: Mapped[int] = mapped_column(Integer, default=0)
    accept_bargain: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_listings_hash_name", "market_hash_name"),
        Index("idx_listings_price", "price_cny"),
        Index("idx_listings_active", "is_active"),
    )


class SteamItem(Base):
    """Предмет из Steam Market (каталог)."""

    __tablename__ = "steam_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sell_listings: Mapped[int] = mapped_column(Integer, default=0)
    sell_price_usd: Mapped[float] = mapped_column(Float, default=0)
    sell_price_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Данные из priceoverview
    median_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    lowest_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Данные из ордеров
    item_nameid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buy_order_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_order_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sell_order_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_order_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Временные метки
    first_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_steam_items_hash", "hash_name"),
    )


class SteamSalesHistory(Base):
    """История продаж Steam Market."""

    __tablename__ = "steam_sales_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash_name: Mapped[str] = mapped_column(Text, nullable=False)
    sale_date: Mapped[str] = mapped_column(Text, nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("hash_name", "sale_date", name="uq_steam_sales_hash_date"),
        Index("idx_steam_sales_hash", "hash_name"),
    )


class ArbitragePurchase(Base):
    """Арбитражные покупки."""

    __tablename__ = "arbitrage_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash_name: Mapped[str] = mapped_column(Text, nullable=False)
    buy_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    steam_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    profit_pct: Mapped[float] = mapped_column(Float, nullable=False)
    daily_sales: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    out_trade_no: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    delivery: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    purchased_at: Mapped[str] = mapped_column(Text, nullable=False)
