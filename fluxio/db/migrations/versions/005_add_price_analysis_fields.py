"""Добавить поля анти-манипуляции в таблицу items.

Revision ID: 005_add_price_analysis_fields
Revises: 004_add_enrichment_fields
Create Date: 2026-03-15

Новые поля для анализа истории продаж:
- price_stability_cv: коэффициент вариации цены (stddev / median)
- sales_at_current_price: кол-во продаж по текущей цене (±20%)
- price_spike_ratio: отношение avg за 3 дня к avg за предыдущие 27 дней
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "005_add_price_analysis_fields"
down_revision = "004_add_enrichment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("price_stability_cv", sa.Float(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("sales_at_current_price", sa.Integer(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("price_spike_ratio", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "price_spike_ratio")
    op.drop_column("items", "sales_at_current_price")
    op.drop_column("items", "price_stability_cv")
