"""region slug is not globally unique

Revision ID: a1b2c3d4e5f6
Revises: f67e2b2fb8ba
Create Date: 2026-11-26 12:55:00.000000

BMS returns duplicate slugs for distinct region codes (for example CHHI and
CIHB both map to "chhibramau"). The code remains the primary key; the slug is
only an index.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f67e2b2fb8ba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_regions_slug", table_name="regions")
    op.create_index("ix_regions_slug", "regions", ["slug"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_regions_slug", table_name="regions")
    op.create_index("ix_regions_slug", "regions", ["slug"], unique=True)
