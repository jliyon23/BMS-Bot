"""Alert delivery hardening: retry scheduling and history retention.

Revision ID: c3d9e7a1b5f2
Revises: a1b2c3d4e5f6
Create Date: 2026-11-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d9e7a1b5f2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_events", recreate="always") as batch:
        batch.alter_column("watch_id", existing_type=sa.Integer(), nullable=True)
        batch.drop_constraint("fk_alert_events_watch_id_watches", type_="foreignkey")
        batch.create_foreign_key(
            "fk_alert_events_watch_id_watches",
            "watches",
            ["watch_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("delete from alert_events where watch_id is null")
    with op.batch_alter_table("alert_events", recreate="always") as batch:
        batch.drop_column("next_attempt_at")
        batch.drop_constraint("fk_alert_events_watch_id_watches", type_="foreignkey")
        batch.create_foreign_key(
            "fk_alert_events_watch_id_watches",
            "watches",
            ["watch_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.alter_column("watch_id", existing_type=sa.Integer(), nullable=False)
