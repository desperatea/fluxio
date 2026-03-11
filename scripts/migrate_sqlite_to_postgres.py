"""Миграция данных из SQLite баз в PostgreSQL (Docker).

Переносит данные из:
  - c5game_bot.db (items, sale_listings)
  - data/dota2_items.db (items, steam_items, steam_sales_history, arbitrage_purchases)

Запуск: python scripts/migrate_sqlite_to_postgres.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import psycopg2  # type: ignore
from dotenv import load_dotenv

# Корень проекта
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

# PostgreSQL подключение
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = os.getenv("POSTGRES_PORT", "5432")
PG_USER = os.getenv("POSTGRES_USER", "bot")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "secret")
PG_DB = os.getenv("POSTGRES_DB", "c5game_bot")

SQLITE_MAIN = ROOT / "c5game_bot.db"
SQLITE_DOTA = ROOT / "data" / "dota2_items.db"

BATCH_SIZE = 5000


def pg_connect() -> psycopg2.extensions.connection:
    """Подключение к PostgreSQL."""
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASS,
        dbname=PG_DB,
    )


def create_tables(pg: psycopg2.extensions.connection) -> None:
    """Создать все таблицы в PostgreSQL."""
    cur = pg.cursor()

    # Общие таблицы (из models.py)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        id SERIAL PRIMARY KEY,
        market_hash_name VARCHAR(255) NOT NULL,
        platform VARCHAR(20) NOT NULL,
        price NUMERIC(10,4) NOT NULL,
        currency VARCHAR(5) NOT NULL,
        sales_count INTEGER,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_price_history_name_time
        ON price_history (market_hash_name, recorded_at DESC);

    CREATE TABLE IF NOT EXISTS purchases (
        id SERIAL PRIMARY KEY,
        product_id VARCHAR(100) NOT NULL UNIQUE,
        order_id VARCHAR(100),
        market_hash_name VARCHAR(255) NOT NULL,
        price_cny NUMERIC(10,4) NOT NULL,
        steam_price_usd NUMERIC(10,4),
        discount_percent NUMERIC(5,2),
        status VARCHAR(50) NOT NULL,
        dry_run BOOLEAN NOT NULL DEFAULT FALSE,
        api_response JSONB,
        purchased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        delivered_at TIMESTAMPTZ,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS active_orders (
        id SERIAL PRIMARY KEY,
        order_id VARCHAR(100) NOT NULL UNIQUE,
        purchase_id BIGINT,
        status VARCHAR(50) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS watchlist (
        id SERIAL PRIMARY KEY,
        market_hash_name VARCHAR(255) NOT NULL UNIQUE,
        max_price_cny NUMERIC(10,4),
        notes TEXT,
        added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    );

    CREATE TABLE IF NOT EXISTS blacklist (
        id SERIAL PRIMARY KEY,
        market_hash_name VARCHAR(255) NOT NULL UNIQUE,
        reason TEXT,
        steam_url TEXT,
        added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS api_logs (
        id SERIAL PRIMARY KEY,
        endpoint VARCHAR(255) NOT NULL,
        method VARCHAR(10) NOT NULL,
        request_body JSONB,
        response_body JSONB,
        status_code INTEGER,
        duration_ms INTEGER,
        logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS config_versions (
        id SERIAL PRIMARY KEY,
        config_yaml TEXT NOT NULL,
        saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        description TEXT
    );

    CREATE TABLE IF NOT EXISTS daily_stats (
        id SERIAL PRIMARY KEY,
        date DATE NOT NULL UNIQUE,
        items_purchased INTEGER NOT NULL DEFAULT 0,
        total_spent_cny NUMERIC(10,4) NOT NULL DEFAULT 0,
        potential_value_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
        potential_profit_usd NUMERIC(10,4) NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS items (
        id SERIAL PRIMARY KEY,
        market_hash_name VARCHAR(255) NOT NULL UNIQUE,
        item_name VARCHAR(500) NOT NULL,
        app_id INTEGER NOT NULL DEFAULT 570,
        cs2dt_item_id BIGINT UNIQUE,
        hero VARCHAR(100),
        slot VARCHAR(50),
        rarity VARCHAR(50),
        quality VARCHAR(50),
        image_url TEXT,
        price_usd NUMERIC(10,4),
        auto_deliver_price_usd NUMERIC(10,4),
        quantity INTEGER NOT NULL DEFAULT 0,
        auto_deliver_quantity INTEGER NOT NULL DEFAULT 0,
        buy_type VARCHAR(20) NOT NULL DEFAULT 'normal',
        min_price_cny NUMERIC(10,4),
        listings_count INTEGER NOT NULL DEFAULT 0,
        steam_price_usd NUMERIC(10,4),
        steam_volume_24h INTEGER,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_items_min_price ON items (min_price_cny);
    CREATE INDEX IF NOT EXISTS idx_items_price_usd ON items (price_usd);
    CREATE INDEX IF NOT EXISTS idx_items_buy_type ON items (buy_type);

    CREATE TABLE IF NOT EXISTS sale_listings (
        id SERIAL PRIMARY KEY,
        c5_id VARCHAR(100) NOT NULL UNIQUE,
        market_hash_name VARCHAR(255) NOT NULL,
        item_name VARCHAR(500) NOT NULL,
        price_cny NUMERIC(10,4) NOT NULL,
        seller_id VARCHAR(100),
        delivery INTEGER NOT NULL DEFAULT 0,
        accept_bargain BOOLEAN NOT NULL DEFAULT FALSE,
        raw_data JSONB,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_listings_hash_name ON sale_listings (market_hash_name);
    CREATE INDEX IF NOT EXISTS idx_listings_price ON sale_listings (price_cny);
    CREATE INDEX IF NOT EXISTS idx_listings_active ON sale_listings (is_active);

    -- Дополнительные таблицы из dota2_items.db
    CREATE TABLE IF NOT EXISTS steam_items (
        id SERIAL PRIMARY KEY,
        hash_name TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        sell_listings INTEGER DEFAULT 0,
        sell_price_usd DOUBLE PRECISION DEFAULT 0,
        sell_price_text TEXT,
        icon_url TEXT,
        item_type TEXT,
        median_price_usd DOUBLE PRECISION,
        lowest_price_usd DOUBLE PRECISION,
        volume_24h INTEGER,
        item_nameid INTEGER,
        buy_order_price_usd DOUBLE PRECISION,
        buy_order_count INTEGER,
        sell_order_price_usd DOUBLE PRECISION,
        sell_order_count INTEGER,
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_steam_items_hash ON steam_items (hash_name);

    CREATE TABLE IF NOT EXISTS steam_sales_history (
        id SERIAL PRIMARY KEY,
        hash_name TEXT NOT NULL,
        sale_date TEXT NOT NULL,
        price_usd DOUBLE PRECISION NOT NULL,
        volume INTEGER NOT NULL,
        UNIQUE(hash_name, sale_date)
    );
    CREATE INDEX IF NOT EXISTS idx_steam_sales_hash ON steam_sales_history (hash_name);

    CREATE TABLE IF NOT EXISTS arbitrage_purchases (
        id SERIAL PRIMARY KEY,
        hash_name TEXT NOT NULL,
        buy_price_usd DOUBLE PRECISION NOT NULL,
        steam_price_usd DOUBLE PRECISION NOT NULL,
        profit_pct DOUBLE PRECISION NOT NULL,
        daily_sales DOUBLE PRECISION,
        order_id TEXT,
        out_trade_no TEXT UNIQUE,
        delivery INTEGER,
        status TEXT DEFAULT 'pending',
        purchased_at TEXT NOT NULL
    );
    """)
    pg.commit()
    print("[OK] Все таблицы созданы в PostgreSQL")


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg: psycopg2.extensions.connection,
    table: str,
    columns: list[str],
    on_conflict: str = "",
) -> int:
    """Перенос данных из SQLite таблицы в PostgreSQL."""
    cur_sq = sqlite_conn.cursor()
    cur_pg = pg.cursor()

    cur_sq.execute(f"SELECT COUNT(*) FROM \"{table}\"")
    total = cur_sq.fetchone()[0]
    if total == 0:
        print(f"  [{table}] Пусто — пропускаю")
        return 0

    cols_str = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"
    if on_conflict:
        insert_sql += f" {on_conflict}"

    cur_sq.execute(f"SELECT {cols_str} FROM \"{table}\"")
    migrated = 0
    skipped = 0

    while True:
        rows = cur_sq.fetchmany(BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            try:
                cur_pg.execute(insert_sql, row)
                migrated += 1
            except psycopg2.errors.UniqueViolation:
                pg.rollback()
                skipped += 1
            except Exception as e:
                pg.rollback()
                skipped += 1
                if skipped <= 3:
                    print(f"    Ошибка: {e}")
        pg.commit()

    suffix = f" (пропущено дубликатов: {skipped})" if skipped else ""
    print(f"  [{table}] {migrated}/{total} записей перенесено{suffix}")
    return migrated


def main() -> None:
    """Основная функция миграции."""
    print("=" * 60)
    print("Миграция SQLite -> PostgreSQL")
    print(f"PostgreSQL: {PG_USER}@{PG_HOST}:{PG_PORT}/{PG_DB}")
    print("=" * 60)

    pg = pg_connect()
    print("[OK] Подключение к PostgreSQL установлено")

    # Создаём таблицы
    create_tables(pg)

    total_migrated = 0
    t0 = time.time()

    # === c5game_bot.db ===
    if SQLITE_MAIN.exists():
        print(f"\n--- {SQLITE_MAIN.name} ---")
        sq = sqlite3.connect(str(SQLITE_MAIN))

        # items (у основной БД нет cs2dt_item_id и других полей — используем подмножество колонок)
        total_migrated += migrate_table(sq, pg, "items", [
            "market_hash_name", "item_name", "app_id",
            "hero", "slot", "rarity", "quality", "image_url",
            "min_price_cny", "listings_count",
            "steam_price_usd", "steam_volume_24h",
            "first_seen_at", "last_seen_at",
        ], on_conflict="ON CONFLICT (market_hash_name) DO NOTHING")

        total_migrated += migrate_table(sq, pg, "sale_listings", [
            "c5_id", "market_hash_name", "item_name", "price_cny",
            "seller_id", "delivery", "accept_bargain", "raw_data",
            "is_active", "scanned_at",
        ], on_conflict="ON CONFLICT (c5_id) DO NOTHING")

        sq.close()
    else:
        print(f"\n[!] {SQLITE_MAIN} не найден — пропускаю")

    # === data/dota2_items.db ===
    if SQLITE_DOTA.exists():
        print(f"\n--- {SQLITE_DOTA.name} ---")
        sq = sqlite3.connect(str(SQLITE_DOTA))

        # items — более полная версия с cs2dt полями, перезаписывает
        total_migrated += migrate_table(sq, pg, "items", [
            "market_hash_name", "item_name", "app_id", "cs2dt_item_id",
            "hero", "slot", "rarity", "quality", "image_url",
            "price_usd", "auto_deliver_price_usd",
            "quantity", "auto_deliver_quantity", "buy_type",
            "min_price_cny", "listings_count",
            "steam_price_usd", "steam_volume_24h",
            "first_seen_at", "last_seen_at",
        ], on_conflict="""ON CONFLICT (market_hash_name) DO UPDATE SET
            cs2dt_item_id = EXCLUDED.cs2dt_item_id,
            price_usd = EXCLUDED.price_usd,
            auto_deliver_price_usd = EXCLUDED.auto_deliver_price_usd,
            quantity = EXCLUDED.quantity,
            auto_deliver_quantity = EXCLUDED.auto_deliver_quantity,
            buy_type = EXCLUDED.buy_type,
            last_seen_at = EXCLUDED.last_seen_at""")

        total_migrated += migrate_table(sq, pg, "steam_items", [
            "hash_name", "name", "sell_listings", "sell_price_usd",
            "sell_price_text", "icon_url", "item_type",
            "median_price_usd", "lowest_price_usd", "volume_24h",
            "item_nameid", "buy_order_price_usd", "buy_order_count",
            "sell_order_price_usd", "sell_order_count",
            "first_seen_at", "updated_at",
        ], on_conflict="ON CONFLICT (hash_name) DO NOTHING")

        # steam_sales_history — самая большая таблица (~2.5M)
        print("  [steam_sales_history] Переносим ~2.5M записей (это займёт время)...")
        total_migrated += migrate_table(sq, pg, "steam_sales_history", [
            "hash_name", "sale_date", "price_usd", "volume",
        ], on_conflict="ON CONFLICT (hash_name, sale_date) DO NOTHING")

        total_migrated += migrate_table(sq, pg, "arbitrage_purchases", [
            "hash_name", "buy_price_usd", "steam_price_usd", "profit_pct",
            "daily_sales", "order_id", "out_trade_no", "delivery",
            "status", "purchased_at",
        ], on_conflict="ON CONFLICT (out_trade_no) DO NOTHING")

        sq.close()
    else:
        print(f"\n[!] {SQLITE_DOTA} не найден — пропускаю")

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"Миграция завершена: {total_migrated} записей за {elapsed:.1f}с")
    print("=" * 60)

    # Проверка
    cur = pg.cursor()
    print("\nПроверка данных в PostgreSQL:")
    for table in [
        "items", "sale_listings", "steam_items",
        "steam_sales_history", "arbitrage_purchases",
    ]:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        cnt = cur.fetchone()[0]
        print(f"  {table}: {cnt} записей")

    pg.close()


if __name__ == "__main__":
    main()
