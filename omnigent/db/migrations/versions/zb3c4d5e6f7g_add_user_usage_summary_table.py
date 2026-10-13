"""add user_usage_summary table for cached usage-page breakdowns

Revision ID: zb3c4d5e6f7g
Revises: mm1a2b3c4d5e
Create Date: 2026-08-30 00:00:00.000000

Adds ``user_usage_summary``: one row per ``(workspace_id, user_id)`` holding
the user's all-time cost-by-harness and cost-by-model maps as JSON text plus a
``needs_rebuild`` flag. Priced turns set the flag; the next first-page
``GET /v1/usage`` rebuilds the row from the user's sessions instead of
scanning them on every request. No backfill: a missing or stale row reads as
a cache miss.

Deployment: additive; older application code ignores the table. Roll back by
downgrading this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb3c4d5e6f7g"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_usage_summary"


def upgrade() -> None:
    # A retry after a partially applied upgrade must not fail on the table.
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("user_id", sa.String(255), primary_key=True, nullable=False),
        # JSON stored as text; MySQL forbids TEXT defaults, so writers supply "{}".
        sa.Column("harness_breakdown", sa.Text(), nullable=False),
        sa.Column("model_breakdown", sa.Text(), nullable=False),
        sa.Column("needs_rebuild", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("total_sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_updated_at", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
