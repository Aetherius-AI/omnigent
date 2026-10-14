"""Connection history upgrades preserve old bindings without inventing evidence."""

from pathlib import Path

import pytest
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
        command.upgrade(config, "mn1a2b3c4d5e")
        row = connection.execute(
            sa.text(
                "SELECT runner_id, runner_last_seen, runner_connected_id "
                "FROM omnigent_conversation_metadata"
            )
        ).one()
        assert tuple(row) == ("old-runner", 1, None)
        for valid in ("x" * 64, "é" * 64, "🙂" * 64):
            connection.execute(
                sa.text("UPDATE omnigent_conversation_metadata SET runner_connected_id = :value"),
                {"value": valid},
            )
        assert len(valid.encode("utf-8")) == 256
        for invalid in (
            "x" * 16_384,
            "x" * 16_385,
            "ok\x00" + "x" * 16_384,
            b"x" * 16_384,
            b"\x00" + b"x" * 16_384,
            "x" * 65,
            "🙂" * 65,
            "ok\x00" + "x" * 80,
            b"short",
        ):
            with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE omnigent_conversation_metadata SET runner_connected_id = :value"
                    ),
                    {"value": invalid},
                )
        command.downgrade(config, "mm1a2b3c4d5e")
        assert "runner_connected_id" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("omnigent_conversation_metadata")
        }
        row = connection.execute(
            sa.text("SELECT runner_id, runner_last_seen FROM omnigent_conversation_metadata")
        ).one()
        assert tuple(row) == ("old-runner", 1)
    engine.dispose()
