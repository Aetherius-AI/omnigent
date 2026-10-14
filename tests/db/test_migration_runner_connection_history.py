"""Connection history upgrades preserve old bindings without inventing evidence."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_runner_connection_history_migration(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'history.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "mm1a2b3c4d5e")
        connection.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata "
                "(workspace_id, id, runner_id, runner_last_seen) VALUES (0, :id, 'old-runner', 1)"
            ),
            {"id": bytes.fromhex("abcdef0123456789abcdef0123456789")},
        )
        command.upgrade(config, "b8d6e1a4c729")
        row = connection.execute(
            sa.text(
                "SELECT runner_id, runner_last_seen, runner_connected_id "
                "FROM omnigent_conversation_metadata"
            )
        ).one()
        assert tuple(row) == ("old-runner", 1, None)
        command.downgrade(config, "mm1a2b3c4d5e")
        assert "runner_connected_id" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("omnigent_conversation_metadata")
        }
        assert (
            connection.execute(
                sa.text("SELECT runner_id FROM omnigent_conversation_metadata")
            ).scalar_one()
            == "old-runner"
        )
    engine.dispose()
