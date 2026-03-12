"""Добавить поле steam_updated_at в таблицу items.

Revision ID: 003_add_steam_updated_at
Revises: 002_rename_cny_to_usd
Create Date: 2026-03-12

Фаза 2: Updater воркер записывает время последнего обновления Steam-цены.
Индекс для фильтрации предметов с устаревшими данными.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "003_add_steam_updated_at"
down_revision = "002_rename_cny_to_usd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Добавить колонку steam_updated_at в items
    op.add_column(
        "items",
        sa.Column(
            "steam_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Индекс для выборки устаревших предметов
    op.create_index(
        "idx_items_steam_updated",
        "items",
        ["steam_updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_items_steam_updated", table_name="items")
    op.drop_column("items", "steam_updated_at")
