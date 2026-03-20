"""Добавить поля Scoring v2 в таблицу items.

Revision ID: 007_add_scoring_fields
Revises: 006_add_steam_orders_fields
Create Date: 2026-03-20

Новые поля для оценки по реальным продажам:
- sell_probability: вероятность прибыльной продажи (0–100)
- weighted_price_usd: взвешенная цена топ-3 точек
- expected_profit_usd: ожидаемая прибыль
- price_concentration: % продаж в полосе ±20% от weighted_price
- p10_price_usd: 10-й перцентиль (пессимистичный сценарий)
- histogram_updated_at: время последнего расчёта scoring
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "007_add_scoring_fields"
down_revision = "006_add_steam_orders_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("sell_probability", sa.Float(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("weighted_price_usd", sa.Float(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("expected_profit_usd", sa.Float(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("price_concentration", sa.Float(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("p10_price_usd", sa.Float(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("histogram_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "histogram_updated_at")
    op.drop_column("items", "p10_price_usd")
    op.drop_column("items", "price_concentration")
    op.drop_column("items", "expected_profit_usd")
    op.drop_column("items", "weighted_price_usd")
    op.drop_column("items", "sell_probability")
