"""Добавить поля Steam ордеров в таблицу items.

Revision ID: 006_add_steam_orders_fields
Revises: 005_add_price_analysis_fields
Create Date: 2026-03-16

Новые поля для данных из itemordershistogram:
- steam_item_nameid: ID предмета для API ордеров (кэшируется навсегда)
- steam_sell_listings: кол-во листингов на продажу
- steam_buy_order_price: максимальная цена ордера на покупку (USD)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "006_add_steam_orders_fields"
down_revision = "005_add_price_analysis_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("steam_item_nameid", sa.Integer(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("steam_sell_listings", sa.Integer(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("steam_buy_order_price", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "steam_buy_order_price")
    op.drop_column("items", "steam_sell_listings")
    op.drop_column("items", "steam_item_nameid")
