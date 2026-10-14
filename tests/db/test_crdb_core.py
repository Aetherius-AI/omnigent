"""Focused tests for CockroachDB engine and schema safeguards."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from alembic import command
from packaging.version import Version
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import NoSuchModuleError

from omnigent.db import cockroachdb, utils


def test_model_schema_revision_matches_model_metadata(tmp_path: Path) -> None:
    """Model tables, columns, and indexes must exist at the revision stamped by bootstrap."""
    from omnigent.db.db_models import MODEL_SCHEMA_REVISION, ConversationBase, OmnigentBase

    uri = f"sqlite:///{tmp_path / 'model_revision.db'}"
    engine = create_engine(uri)
    try:
        config = utils._build_alembic_config(uri)
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, MODEL_SCHEMA_REVISION)
        inspector = inspect(engine)
        migrated = {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
            if table != "alembic_version"
        }
        models = {
            table.name: set(table.columns.keys())
            for metadata in (OmnigentBase.metadata, ConversationBase.metadata)
            for table in metadata.tables.values()
        }
        assert migrated == models, (
            "Advance MODEL_SCHEMA_REVISION with model table/column changes; "
            "CockroachDB bootstrap stamps this revision before later migrations."
        )
        migrated_indexes = {
            table: {
                str(index["name"])
                for index in inspector.get_indexes(table)
                if index.get("name") and not str(index["name"]).startswith("sqlite_autoindex")
            }
            for table in migrated
        }
        model_indexes = {
            table.name: {
                index.name
                for index in table.indexes
                # Dialect-gated indexes (the CockroachDB trigram index) never exist on SQLite.
                if index.name and index._ddl_if is None
            }
            for metadata in (OmnigentBase.metadata, ConversationBase.metadata)
            for table in metadata.tables.values()
        }
        assert migrated_indexes == model_indexes, (
            "Advance MODEL_SCHEMA_REVISION with model index changes; "
            "CockroachDB bootstrap creates model indexes before later migrations."
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("off_chain", ["not-a-revision", "before-baseline"])
def test_initialize_rejects_model_revision_off_migration_chain(
    off_chain: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared model revision must sit between the CRDB baseline and head."""
    from alembic.script import ScriptDirectory

    from omnigent.db import db_models

    uri = "sqlite:///unused.db"
    if off_chain == "before-baseline":
        script = ScriptDirectory.from_config(utils._build_alembic_config(uri))
        down = script.get_revision(cockroachdb.CRDB_BASELINE_REVISION).down_revision
        assert isinstance(down, str)
        off_chain = down
    engine = MagicMock()
    monkeypatch.setattr(cockroachdb, "_crdb_server_version", lambda _engine: Version("25.2.10"))
    monkeypatch.setattr(cockroachdb, "_verify_crdb_read_committed", lambda _engine, _version: None)
    monkeypatch.setattr(db_models, "MODEL_SCHEMA_REVISION", off_chain)

    with pytest.raises(RuntimeError, match="not on the configured migration chain"):
        cockroachdb._initialize_or_verify_crdb_schema(engine, uri)

    engine.connect.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CockroachDB v23.2.28 (x86_64)", Version("23.2.28")),
        ("CockroachDB CCL v24.3.20 (x86_64)", Version("24.3.20")),
        ("CockroachDB OSS v25.2.10 (x86_64)", Version("25.2.10")),
        ("CockroachDB Enterprise v25.4.5 (x86_64)", Version("25.4.5")),
    ],
)
def test_parse_crdb_server_version_accepts_distribution_labels(
    raw: str,
    expected: Version,
) -> None:
    assert cockroachdb._parse_crdb_server_version(raw) == expected


def test_parse_crdb_server_version_rejects_versions_below_minimum() -> None:
    with pytest.raises(RuntimeError, match=r"requires CockroachDB 23\.2\.28 or newer"):
        cockroachdb._parse_crdb_server_version("CockroachDB OSS v23.2.27 (x86_64)")


def test_parse_crdb_server_version_warns_outside_tested_matrix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    version = cockroachdb._parse_crdb_server_version("CockroachDB CCL v26.1.0 (x86_64)")

    assert version == Version("26.1.0")
    assert "outside Omnigent's release-tested matrix" in caplog.text


def test_crdb_engine_reports_missing_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_dialect(_uri: str, **_kwargs: Any) -> None:
        raise NoSuchModuleError("cockroachdb.psycopg")

    monkeypatch.setattr(utils, "create_engine", missing_dialect)

    with pytest.raises(RuntimeError, match=r"omnigent\[cockroachdb\]"):
        utils._create_engine("cockroachdb://root@localhost/defaultdb")


def test_verify_crdb_read_committed_rejects_silent_v23_fallback() -> None:
    result = MagicMock()
    result.scalar_one.return_value = "serializable"
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    with pytest.raises(RuntimeError, match=r"sql\.txn\.read_committed_isolation\.enabled"):
        cockroachdb._verify_crdb_read_committed(engine, Version("23.2.28"))


def test_verify_crdb_read_committed_accepts_effective_level() -> None:
    result = MagicMock()
    result.scalar_one.return_value = "read committed"
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    cockroachdb._verify_crdb_read_committed(engine, Version("23.2.28"))


def test_crdb_pool_overrides_treat_blank_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OMNIGENT_DB_POOL_SIZE", " ")
    monkeypatch.setenv("OMNIGENT_DB_MAX_OVERFLOW", "")
    monkeypatch.setenv("OMNIGENT_DB_POOL_TIMEOUT", "\t")
    monkeypatch.setattr(
        utils,
        "create_engine",
        lambda _uri, **kwargs: captured.update(kwargs) or MagicMock(),
    )

    utils._create_engine("cockroachdb://root@localhost/defaultdb")

    assert captured["pool_size"] == 200
    assert captured["max_overflow"] == 20
    assert captured["pool_timeout"] == 10.0


def test_crdb_pool_overrides_accept_sqlalchemy_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OMNIGENT_DB_POOL_SIZE", "0")
    monkeypatch.setenv("OMNIGENT_DB_MAX_OVERFLOW", "-1")
    monkeypatch.setenv("OMNIGENT_DB_POOL_TIMEOUT", "0.25")
    monkeypatch.setattr(
        utils,
        "create_engine",
        lambda _uri, **kwargs: captured.update(kwargs) or MagicMock(),
    )

    utils._create_engine("cockroachdb://root@localhost/defaultdb")

    assert captured["pool_size"] == 0
    assert captured["max_overflow"] == -1
    assert captured["pool_timeout"] == 0.25


def test_prepare_crdb_schema_transaction_uses_serializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    enabled: list[Version] = []
    monkeypatch.setattr(
        cockroachdb,
        "_enable_crdb_ddl_autocommit",
        lambda _connection, version: enabled.append(version),
    )

    cockroachdb._prepare_crdb_schema_transaction(connection, Version("24.3.20"))

    assert enabled == [Version("24.3.20")]
    statement = str(connection.execute.call_args.args[0])
    assert statement == "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"


def test_bootstrap_rejects_unknown_unversioned_tables() -> None:
    engine = MagicMock()

    with pytest.raises(RuntimeError, match="Use a new empty database"):
        cockroachdb._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {"agents"},
            {"agents", "conversations"},
            "head",
        )

    engine.connect.assert_not_called()


def test_bootstrap_resumes_only_with_valid_marker() -> None:
    result = MagicMock()
    result.__iter__.return_value = iter([(cockroachdb._CRDB_BOOTSTRAP_MARKER_TOKEN, "head")])
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    cockroachdb._start_or_resume_crdb_bootstrap(
        engine,
        Version("25.2.10"),
        {cockroachdb._CRDB_BOOTSTRAP_MARKER_TABLE, "alembic_version", "agents"},
        {"agents", "conversations"},
        "head",
    )


def test_bootstrap_rejects_invalid_marker() -> None:
    result = MagicMock()
    result.__iter__.return_value = iter([("not-an-omnigent-marker", "head")])
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    with pytest.raises(RuntimeError, match="invalid Omnigent bootstrap marker"):
        cockroachdb._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {cockroachdb._CRDB_BOOTSTRAP_MARKER_TABLE, "agents"},
            {"agents", "conversations"},
            "head",
        )


@pytest.mark.parametrize(
    ("previous_target", "current_target"),
    [
        ("older-head", "current-head"),
        ("old-model:head", "new-model:head"),
        ("head", "model:head"),
    ],
)
def test_bootstrap_rejects_marker_from_different_schema_target(
    previous_target: str, current_target: str
) -> None:
    result = MagicMock()
    result.__iter__.return_value = iter(
        [(cockroachdb._CRDB_BOOTSTRAP_MARKER_TOKEN, previous_target)]
    )
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    with pytest.raises(RuntimeError, match="invalid Omnigent bootstrap marker") as error:
        cockroachdb._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {cockroachdb._CRDB_BOOTSTRAP_MARKER_TABLE, "agents"},
            {"agents", "conversations"},
            current_target,
        )
    assert "Existing data was left unchanged" in str(error.value)
    assert "Back up and inspect" in str(error.value)
    assert all(
        str(call.args[0]).startswith("SELECT") for call in connection.execute.call_args_list
    )


def test_bootstrap_rejects_empty_alembic_table_without_marker() -> None:
    engine = MagicMock()

    with pytest.raises(RuntimeError, match="Use a new empty database"):
        cockroachdb._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {"alembic_version"},
            {"agents", "conversations"},
            "head",
        )


def test_bootstrap_repairs_and_verifies_missing_model_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Table:
        def __init__(self, name: str) -> None:
            self.name = name

    class Index:
        def __init__(self, name: str, table: str, *, created: bool) -> None:
            self.name = name
            self.table = Table(table)
            self.created = created

        def create(self, *, bind: object, checkfirst: bool) -> None:
            assert bind is connection
            assert checkfirst is True
            self.created = True

    existing = Index("ix_agents_created_at", "agents", created=True)
    missing = Index("ix_agents_name", "agents", created=False)

    class Inspector:
        def get_indexes(self, table_name: str) -> list[dict[str, str]]:
            assert table_name == "agents"
            return [{"name": existing.name}]

        def has_index(self, table_name: str, index_name: str) -> bool:
            assert table_name == "agents"
            return index_name == existing.name or missing.created

    connection = MagicMock()
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)
    prepare = MagicMock()
    monkeypatch.setattr(cockroachdb, "_crdb_model_indexes", lambda: (existing, missing))
    monkeypatch.setattr(cockroachdb, "inspect", lambda _engine: Inspector())
    monkeypatch.setattr(cockroachdb, "_prepare_crdb_schema_transaction", prepare)

    cockroachdb._repair_and_verify_crdb_model_indexes(engine, Version("25.2.10"))

    assert missing.created is True
    prepare.assert_called_once_with(connection, Version("25.2.10"))
    connection.commit.assert_called_once_with()
