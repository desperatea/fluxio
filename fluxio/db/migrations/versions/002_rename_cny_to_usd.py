"""Переименование колонок price_cny → price_usd.

CS2DT — основная площадка, работает в USD.
Все цены теперь хранятся в USD.

Revision ID: 002_rename_cny_to_usd
Revises: 001_initial
Create Date: 2026-03-12
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002_rename_cny_to_usd"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # purchases: price_cny → price_usd
    op.alter_column("purchases", "price_cny", new_column_name="price_usd")

    # daily_stats: total_spent_cny → total_spent_usd
    op.alter_column("daily_stats", "total_spent_cny", new_column_name="total_spent_usd")

    # watchlist: max_price_cny → max_price_usd
    op.alter_column("watchlist", "max_price_cny", new_column_name="max_price_usd")

    # items: min_price_cny → min_price_usd_market
    op.drop_index("idx_items_min_price", table_name="items")
    op.alter_column("items", "min_price_cny", new_column_name="min_price_usd_market")
    op.create_index("idx_items_min_price", "items", ["min_price_usd_market"])

    # sale_listings: price_cny → price_usd
    op.drop_index("idx_listings_price", table_name="sale_listings")
    op.alter_column("sale_listings", "price_cny", new_column_name="price_usd")
    op.create_index("idx_listings_price", "sale_listings", ["price_usd"])


def downgrade() -> None:
    # sale_listings: price_usd → price_cny
    op.drop_index("idx_listings_price", table_name="sale_listings")
    op.alter_column("sale_listings", "price_usd", new_column_name="price_cny")
    op.create_index("idx_listings_price", "sale_listings", ["price_cny"])

    # items: min_price_usd_market → min_price_cny
    op.drop_index("idx_items_min_price", table_name="items")
    op.alter_column("items", "min_price_usd_market", new_column_name="min_price_cny")
    op.create_index("idx_items_min_price", "items", ["min_price_cny"])

    # watchlist: max_price_usd → max_price_cny
    op.alter_column("watchlist", "max_price_usd", new_column_name="max_price_cny")

    # daily_stats: total_spent_usd → total_spent_cny
    op.alter_column("daily_stats", "total_spent_usd", new_column_name="total_spent_cny")

    # purchases: price_usd → price_cny
    op.alter_column("purchases", "price_usd", new_column_name="price_cny")
