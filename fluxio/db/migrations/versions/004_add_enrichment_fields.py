"""Добавить поля обогащения (enrichment) в таблицу items.

Revision ID: 004_add_enrichment_fields
Revises: 003_add_steam_updated_at
Create Date: 2026-03-14

Новые поля для хранения 30-дневной медианы из pricehistory:
- steam_median_30d: медианная цена за 30 дней (USD)
- steam_volume_7d: объём продаж за 7 дней
- enriched_at: когда предмет был последний раз обогащён
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "004_add_enrichment_fields"
down_revision = "003_add_steam_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("steam_median_30d", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("steam_volume_7d", sa.Integer(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column(
            "enriched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Индекс для выборки необогащённых предметов
    op.create_index(
        "idx_items_enriched_at",
        "items",
        ["enriched_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_items_enriched_at", table_name="items")
    op.drop_column("items", "enriched_at")
    op.drop_column("items", "steam_volume_7d")
    op.drop_column("items", "steam_median_30d")
