"""Live CockroachDB compatibility checks selected by OMNIGENT_TEST_DB_URI.

The fresh-bootstrap scenario also runs on SQLite as a stand-in that stubs only
the CockroachDB connection preamble, so the bootstrap orchestration stays
covered where no CockroachDB server is available.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from packaging.version import Version
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from omnigent.db import cockroachdb, utils
from omnigent.db.cockroachdb import (
    _CRDB_BOOTSTRAP_MARKER_TABLE,
    _CRDB_BOOTSTRAP_MARKER_TOKEN,
    CRDB_BASELINE_REVISION,
    _crdb_server_version,
    _initialize_or_verify_crdb_schema,
    _prepare_crdb_schema_transaction,
)
from omnigent.db.db_models import MODEL_SCHEMA_REVISION
from omnigent.db.utils import (
    _build_alembic_config,
    _create_engine,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    get_or_create_engine,
    is_cockroachdb,
)

_PACKAGED_VERSIONS = Path(utils.__file__).parent / "migrations" / "versions"
_SCHEMA_ONLY_REVISION = "schemaonly0001"
_SCHEMA_ONLY_COLUMN = "schema_only_probe"


def _crdb_engine(db_uri: str) -> Engine:
    engine = get_or_create_engine(db_uri)
    if not is_cockroachdb(engine.dialect.name):
        pytest.skip("requires OMNIGENT_TEST_DB_URI pointing to CockroachDB")
    return engine


def _stub_crdb_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    # SQLite cannot answer the CockroachDB-only probes that run before the
    # bootstrap DDL, nor build the CockroachDB-only trigram index.
    monkeypatch.setattr(cockroachdb, "_crdb_server_version", lambda _engine: Version("23.2.28"))
    monkeypatch.setattr(cockroachdb, "_verify_crdb_read_committed", lambda _engine, _version: None)
    monkeypatch.setattr(
        cockroachdb, "_prepare_crdb_schema_transaction", lambda _connection, _version: None
    )
    model_indexes = cockroachdb._crdb_model_indexes
    monkeypatch.setattr(
        cockroachdb,
        "_crdb_model_indexes",
        lambda: tuple(index for index in model_indexes() if index._ddl_if is None),
    )


def _add_schema_only_migration(
    tmp_path: Path, down_revision: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ship a nullable ``agents`` column by migration only; the ORM stays unmapped."""
    extra = tmp_path / "schema_only_versions"
    extra.mkdir()
    (extra / f"{_SCHEMA_ONLY_REVISION}_add_schema_only_probe.py").write_text(
        f'''"""Add a nullable column before its model mapping ships."""

import sqlalchemy as sa
from alembic import op

revision = "{_SCHEMA_ONLY_REVISION}"
down_revision = "{down_revision}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("{_SCHEMA_ONLY_COLUMN}", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "{_SCHEMA_ONLY_COLUMN}")
'''
    )
    build_config = utils._build_alembic_config

    def build_extended_config(db_uri: str):
        config = build_config(db_uri)
        config.set_main_option("path_separator", "os")
        config.set_main_option(
            "version_locations", os.pathsep.join((str(_PACKAGED_VERSIONS), str(extra)))
        )
        return config

    monkeypatch.setattr(utils, "_build_alembic_config", build_extended_config)


def _fresh_database(db_uri: str, tmp_path: Path) -> tuple[str, Callable[[], None]]:
    url = make_url(db_uri)
    if url.get_backend_name() == "sqlite":
        return f"sqlite:///{tmp_path / 'fresh-bootstrap.db'}", lambda: None
    name = f"{url.database}_bootstrap"
    root = create_engine(db_uri, isolation_level="AUTOCOMMIT")

    def run(statement: str) -> None:
        with root.connect() as connection:
            connection.execute(text(statement))

    def drop() -> None:
        run(f'DROP DATABASE IF EXISTS "{name}" CASCADE')
        root.dispose()

    try:
        run(f'DROP DATABASE IF EXISTS "{name}" CASCADE')
        run(f'CREATE DATABASE "{name}"')
    except Exception:
        root.dispose()
        raise
    return url.set(database=name).render_as_string(hide_password=False), drop


def test_cockroachdb_bootstrap_applies_schema_only_migration(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialect = get_or_create_engine(db_uri).dialect.name
    if dialect not in {"cockroachdb", "sqlite"}:
        pytest.skip("runs on CockroachDB, or on SQLite as a stand-in for its bootstrap")
    if dialect == "sqlite":
        _stub_crdb_preamble(monkeypatch)
    model_head = _get_head_db_revision(db_uri)
    _add_schema_only_migration(tmp_path, model_head, monkeypatch)
    fresh_uri, drop_database = _fresh_database(db_uri, tmp_path)
    engine = _create_engine(fresh_uri)

    def agents_columns() -> set[str]:
        return {column["name"] for column in inspect(engine).get_columns("agents")}

    def downgrade_to(revision: str) -> None:
        config = utils._build_alembic_config(fresh_uri)
        with engine.connect() as connection:
            cockroachdb._prepare_crdb_schema_transaction(
                connection, cockroachdb._crdb_server_version(engine)
            )
            config.attributes["connection"] = connection
            command.downgrade(config, revision)
            connection.commit()

    try:
        _initialize_or_verify_crdb_schema(engine, fresh_uri)

        assert _get_current_db_revision(engine) == _SCHEMA_ONLY_REVISION
        assert _CRDB_BOOTSTRAP_MARKER_TABLE not in inspect(engine).get_table_names()
        assert _SCHEMA_ONLY_COLUMN in agents_columns()

        downgrade_to(model_head)
        assert _get_current_db_revision(engine) == model_head
        assert _SCHEMA_ONLY_COLUMN not in agents_columns()

        _initialize_or_verify_crdb_schema(engine, fresh_uri)
        assert _get_current_db_revision(engine) == _SCHEMA_ONLY_REVISION
        assert _SCHEMA_ONLY_COLUMN in agents_columns()
    finally:
        engine.dispose()
        drop_database()


def test_cockroachdb_bootstrap_resumes_after_interrupted_migration(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption after the model stamp finishes through the ordinary upgrade path."""
    dialect = get_or_create_engine(db_uri).dialect.name
    if dialect not in {"cockroachdb", "sqlite"}:
        pytest.skip("runs on CockroachDB, or on SQLite as a stand-in for its bootstrap")
    if dialect == "sqlite":
        _stub_crdb_preamble(monkeypatch)
    model_head = _get_head_db_revision(db_uri)
    _add_schema_only_migration(tmp_path, model_head, monkeypatch)
    fresh_uri, drop_database = _fresh_database(db_uri, tmp_path)
    engine = _create_engine(fresh_uri)
    run_migrations = utils._run_migrations
    attempts = 0

    def interrupted_run_migrations(target: Engine, uri: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected migration interruption")
        run_migrations(target, uri)

    monkeypatch.setattr(utils, "_run_migrations", interrupted_run_migrations)

    def agents_columns() -> set[str]:
        return {column["name"] for column in inspect(engine).get_columns("agents")}

    try:
        with pytest.raises(RuntimeError, match="schema migration failed") as failure:
            _initialize_or_verify_crdb_schema(engine, fresh_uri)
        assert "injected migration interruption" in str(failure.value.__cause__)
        assert _get_current_db_revision(engine) == MODEL_SCHEMA_REVISION
        assert _CRDB_BOOTSTRAP_MARKER_TABLE in inspect(engine).get_table_names()
        assert _SCHEMA_ONLY_COLUMN not in agents_columns()

        _initialize_or_verify_crdb_schema(engine, fresh_uri)

        assert attempts == 2
        assert _get_current_db_revision(engine) == _SCHEMA_ONLY_REVISION
        assert _SCHEMA_ONLY_COLUMN in agents_columns()
        assert _CRDB_BOOTSTRAP_MARKER_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        drop_database()


def test_cockroachdb_bootstrap_is_at_head(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)


def test_cockroachdb_uses_read_committed(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    with engine.connect() as connection:
        isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()
    assert str(isolation).lower() == "read committed"


def test_cockroachdb_search_indexes_exist(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    expected = {"ix_conversations_title_trgm"}
    found: set[str] = set()
    with engine.connect() as connection:
        for table in ("conversation_items", "conversations"):
            found.update(
                str(row["index_name"])
                for row in connection.execute(text(f"SHOW INDEXES FROM {table}")).mappings()
            )
    assert expected <= found
    assert "ix_conversation_items_search_text_trgm" not in found


def test_cockroachdb_upgrades_from_supported_baseline(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    head = _get_head_db_revision(db_uri)
    if head == CRDB_BASELINE_REVISION:
        pytest.skip("requires a migration after the CRDB baseline")

    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        command.downgrade(config, CRDB_BASELINE_REVISION)
        connection.commit()

    assert _get_current_db_revision(engine) == CRDB_BASELINE_REVISION

    _initialize_or_verify_schema(engine, db_uri)

    assert _get_current_db_revision(engine) == head


def test_cockroachdb_resumes_empty_revision_and_repairs_indexes(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    version = _crdb_server_version(engine)
    head = _get_head_db_revision(db_uri)
    index_name = "ix_agents_created_at"
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, version)
        config = _build_alembic_config(db_uri)
        config.attributes["connection"] = connection
        # Interrupted model creation has not applied schema-only revisions yet.
        command.downgrade(config, MODEL_SCHEMA_REVISION)
        connection.commit()
        _prepare_crdb_schema_transaction(connection, version)
        connection.execute(
            text(
                f"CREATE TABLE {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
            )
        )
        connection.commit()
        _prepare_crdb_schema_transaction(connection, version)
        connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        connection.commit()
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                "(token, target_revision) VALUES (:token, :target_revision)"
            ),
            {
                "token": _CRDB_BOOTSTRAP_MARKER_TOKEN,
                "target_revision": f"{MODEL_SCHEMA_REVISION}:{head}",
            },
        )
        connection.execute(text("DELETE FROM alembic_version"))

    _initialize_or_verify_schema(engine, db_uri)

    with engine.connect() as connection:
        found = {
            str(row["index_name"])
            for row in connection.execute(text("SHOW INDEXES FROM agents")).mappings()
        }
    assert _get_current_db_revision(engine) == head
    assert _CRDB_BOOTSTRAP_MARKER_TABLE not in inspect(engine).get_table_names()
    assert index_name in found


def test_account_generation_backfill_resumes_after_schema_commit(db_uri) -> None:
    from sqlalchemy import event

    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.device_grant_store import DeviceGrantStore

    engine = _crdb_engine(db_uri)
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("migration-user", "existing-password-hash")
    grants = DeviceGrantStore(db_uri)
    grants.create_redeemed_grant(
        "migration-grant",
        user_id="migration-user",
        client_id="cli",
        refresh_token_hash="existing-refresh-hash",
        created_at=100,
    )
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        command.downgrade(config, "hh1b2c3d4e5f")
        connection.commit()

    def interrupt_backfill(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE users SET account_generation"):
            raise RuntimeError("injected backfill interruption")

    event.listen(engine, "before_cursor_execute", interrupt_backfill)
    try:
        with pytest.raises(RuntimeError, match="schema migration failed"):
            _initialize_or_verify_schema(engine, db_uri)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_backfill)
    assert _get_current_db_revision(engine) == "hh1b2c3d4e5f"
    assert "account_generation" in {c["name"] for c in inspect(engine).get_columns("users")}

    _initialize_or_verify_schema(engine, db_uri)
    account = accounts.get_user("migration-user")
    assert account is not None and len(account.account_generation) == 32
    assert accounts.get_password_hash("migration-user") == "existing-password-hash"
    grant = grants.authorize_access("migration-grant")
    assert grant is not None and grant.account_generation == account.account_generation
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)


@pytest.mark.parametrize("failed_attempts", [2, 3])
def test_project_preferences_copy_retries_serialization_failures(
    db_uri: str, failed_attempts: int, capfd: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy import event

    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    engine = _crdb_engine(db_uri)
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("ordering-user", "existing-password-hash")
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        command.downgrade(config, "ii1a2b3c4d5e")
        connection.commit()

    payload = b"legacy project order bytes"
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET project_order = :value WHERE id = 'ordering-user'"),
            {"value": payload},
        )

    attempts = 0

    def fail_copy(conn, cursor, statement, parameters, context, executemany):
        nonlocal attempts
        if statement.startswith("INSERT INTO preferences"):
            attempts += 1
            if attempts <= failed_attempts:
                # Observed results prevent transparent server retries of a fresh transaction.
                cursor.execute("SELECT id FROM users WHERE id = 'ordering-user'")
                cursor.fetchone()
                return "SELECT crdb_internal.force_retry('10s')", ()
        return statement, parameters

    event.listen(engine, "before_cursor_execute", fail_copy, retval=True)
    try:
        _initialize_or_verify_schema(engine, db_uri)
    finally:
        event.remove(engine, "before_cursor_execute", fail_copy)

    assert attempts == 3
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)
    assert "project_order" not in {c["name"] for c in inspect(engine).get_columns("users")}
    with engine.connect() as connection:
        saved = connection.execute(
            text(
                "SELECT value FROM preferences "
                "WHERE user_id = 'ordering-user' AND key = 'project_order'"
            )
        ).scalar_one_or_none()
    assert saved == (payload if failed_attempts == 2 else None)
    warning = "Could not migrate project order preferences after 3 attempts"
    assert (warning in capfd.readouterr().err) == (failed_attempts == 3)
