"""Retain connection evidence for the runner bound to a session.

Revision ID: b8d6e1a4c729
Revises: mm1a2b3c4d5e
Create Date: 2026-10-14 00:00:00.000000

Adds ``runner_connected_id`` (nullable String(64)) to
``omnigent_conversation_metadata``: the bound ``runner_id`` whose tunnel
heartbeat was persisted, kept when the live ``runner_last_seen`` stamp is
cleared on disconnect. Existing rows stay NULL; connection history is not
inferred from old heartbeats.

Deployment: the column is additive and nullable. Apply this revision before
deploying an application reader or writer for the column.
Older servers ignore it, so application rollback needs no schema change.
Downgrade only after all readers and writers have been rolled back.
See ``docs/runner-connection-history.md`` for rollout and rollback details.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d6e1a4c729"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("runner_connected_id", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("runner_connected_id")
