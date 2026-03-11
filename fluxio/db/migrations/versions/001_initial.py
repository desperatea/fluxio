"""Initial migration — все таблицы из models.py.

Revision ID: 001_initial
Revises: None
Create Date: 2026-03-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # price_history
    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("market_hash_name", sa.String(255), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("price", sa.Numeric(10, 4), nullable=False),
        sa.Column("currency", sa.String(5), nullable=False),
        sa.Column("sales_count", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_price_history_name_time", "price_history", ["market_hash_name", sa.text("recorded_at DESC")])

    # purchases
    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("product_id", sa.String(100), nullable=False, unique=True),
        sa.Column("order_id", sa.String(100), nullable=True),
        sa.Column("market_hash_name", sa.String(255), nullable=False),
        sa.Column("price_cny", sa.Numeric(10, 4), nullable=False),
        sa.Column("steam_price_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("dry_run", sa.Boolean(), default=False),
        sa.Column("api_response", sa.JSON(), nullable=True),
        sa.Column("purchased_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # active_orders
    op.create_table(
        "active_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("order_id", sa.String(100), nullable=False, unique=True),
        sa.Column("purchase_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # watchlist
    op.create_table(
        "watchlist",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("market_hash_name", sa.String(255), nullable=False, unique=True),
        sa.Column("max_price_cny", sa.Numeric(10, 4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("is_active", sa.Boolean(), default=True),
    )

    # blacklist
    op.create_table(
        "blacklist",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("market_hash_name", sa.String(255), nullable=False, unique=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("steam_url", sa.Text(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # api_logs
    op.create_table(
        "api_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("request_body", sa.JSON(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # config_versions
    op.create_table(
        "config_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("config_yaml", sa.Text(), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("description", sa.Text(), nullable=True),
    )

    # daily_stats
    op.create_table(
        "daily_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("date", sa.Date(), nullable=False, unique=True),
        sa.Column("items_purchased", sa.Integer(), default=0),
        sa.Column("total_spent_cny", sa.Numeric(10, 4), default=0),
        sa.Column("potential_value_usd", sa.Numeric(10, 4), default=0),
        sa.Column("potential_profit_usd", sa.Numeric(10, 4), default=0),
    )

    # items
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("market_hash_name", sa.String(255), nullable=False, unique=True),
        sa.Column("item_name", sa.String(500), nullable=False),
        sa.Column("app_id", sa.Integer(), nullable=False, server_default="570"),
        sa.Column("cs2dt_item_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("hero", sa.String(100), nullable=True),
        sa.Column("slot", sa.String(50), nullable=True),
        sa.Column("rarity", sa.String(50), nullable=True),
        sa.Column("quality", sa.String(50), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("price_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("auto_deliver_price_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("quantity", sa.Integer(), default=0),
        sa.Column("auto_deliver_quantity", sa.Integer(), default=0),
        sa.Column("buy_type", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("min_price_cny", sa.Numeric(10, 4), nullable=True),
        sa.Column("listings_count", sa.Integer(), default=0),
        sa.Column("steam_price_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("steam_volume_24h", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_items_min_price", "items", ["min_price_cny"])
    op.create_index("idx_items_price_usd", "items", ["price_usd"])
    op.create_index("idx_items_buy_type", "items", ["buy_type"])

    # sale_listings
    op.create_table(
        "sale_listings",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("c5_id", sa.String(100), nullable=False, unique=True),
        sa.Column("market_hash_name", sa.String(255), nullable=False),
        sa.Column("item_name", sa.String(500), nullable=False),
        sa.Column("price_cny", sa.Numeric(10, 4), nullable=False),
        sa.Column("seller_id", sa.String(100), nullable=True),
        sa.Column("delivery", sa.Integer(), default=0),
        sa.Column("accept_bargain", sa.Boolean(), default=False),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_listings_hash_name", "sale_listings", ["market_hash_name"])
    op.create_index("idx_listings_price", "sale_listings", ["price_cny"])
    op.create_index("idx_listings_active", "sale_listings", ["is_active"])

    # steam_items
    op.create_table(
        "steam_items",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("hash_name", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sell_listings", sa.Integer(), default=0),
        sa.Column("sell_price_usd", sa.Float(), default=0),
        sa.Column("sell_price_text", sa.Text(), nullable=True),
        sa.Column("icon_url", sa.Text(), nullable=True),
        sa.Column("item_type", sa.Text(), nullable=True),
        sa.Column("median_price_usd", sa.Float(), nullable=True),
        sa.Column("lowest_price_usd", sa.Float(), nullable=True),
        sa.Column("volume_24h", sa.Integer(), nullable=True),
        sa.Column("item_nameid", sa.Integer(), nullable=True),
        sa.Column("buy_order_price_usd", sa.Float(), nullable=True),
        sa.Column("buy_order_count", sa.Integer(), nullable=True),
        sa.Column("sell_order_price_usd", sa.Float(), nullable=True),
        sa.Column("sell_order_count", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_steam_items_hash", "steam_items", ["hash_name"])

    # steam_sales_history
    op.create_table(
        "steam_sales_history",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("hash_name", sa.Text(), nullable=False),
        sa.Column("sale_date", sa.Text(), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.UniqueConstraint("hash_name", "sale_date", name="uq_steam_sales_hash_date"),
    )
    op.create_index("idx_steam_sales_hash", "steam_sales_history", ["hash_name"])

    # arbitrage_purchases
    op.create_table(
        "arbitrage_purchases",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("hash_name", sa.Text(), nullable=False),
        sa.Column("buy_price_usd", sa.Float(), nullable=False),
        sa.Column("steam_price_usd", sa.Float(), nullable=False),
        sa.Column("profit_pct", sa.Float(), nullable=False),
        sa.Column("daily_sales", sa.Float(), nullable=True),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column("out_trade_no", sa.Text(), nullable=True, unique=True),
        sa.Column("delivery", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending"),
        sa.Column("purchased_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("arbitrage_purchases")
    op.drop_table("steam_sales_history")
    op.drop_table("steam_items")
    op.drop_table("sale_listings")
    op.drop_table("items")
    op.drop_table("daily_stats")
    op.drop_table("config_versions")
    op.drop_table("api_logs")
    op.drop_table("blacklist")
    op.drop_table("watchlist")
    op.drop_table("active_orders")
    op.drop_table("purchases")
    op.drop_table("price_history")
