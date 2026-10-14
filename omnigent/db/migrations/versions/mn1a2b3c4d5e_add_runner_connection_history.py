"""Retain connection evidence for the runner bound to a session.

Revision ID: mn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-14 00:00:00.000000

Adds ``runner_connected_id`` (nullable String(64)) to
``omnigent_conversation_metadata``: the bound ``runner_id`` whose tunnel
heartbeat was persisted, kept when the live ``runner_last_seen`` stamp is
cleared on disconnect. Existing rows stay NULL; connection history is not
inferred from old heartbeats.
SQLite also enforces the 64-character limit and its 256-byte UTF-8 bound,
rejecting binary values and embedded NULs. Other backends enforce VARCHAR(64).

Deployment: the column is additive and nullable. Apply this revision before
deploying an application reader or writer for the column.
For application rollback, use the schema-only build that knows this revision.
Downgrade only after all readers and writers have been rolled back.
See ``docs/runner-connection-history.md`` for rollout and rollback details.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQLITE_CHECK_NAME = "ck_conversation_metadata_runner_connected_id_bound"
_SQLITE_CHECK = (
    "runner_connected_id IS NULL OR ("
    "typeof(runner_connected_id) = 'text' "
    "AND instr(runner_connected_id, char(0)) = 0 "
    "AND length(runner_connected_id) <= 64 "
    "AND length(CAST(runner_connected_id AS BLOB)) <= 256)"
)


def upgrade() -> None:
    constraints = (
        [sa.CheckConstraint(_SQLITE_CHECK, name=_SQLITE_CHECK_NAME)]
        if op.get_bind().dialect.name == "sqlite"
        else []
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(
            sa.Column("runner_connected_id", sa.String(64), *constraints, nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        if op.get_bind().dialect.name == "sqlite":
            batch_op.drop_constraint(_SQLITE_CHECK_NAME, type_="check")
        batch_op.drop_column("runner_connected_id")
