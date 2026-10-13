"""Migration ``zb3c4d5e6f7g`` adds ``user_usage_summary``.

The usage page's breakdown cache lives in this table, so it must exist at head
keyed by ``(workspace_id, user_id)``, and the downgrade must remove it.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_TABLE = "user_usage_summary"


def test_table_at_head(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        inspector = sa.inspect(engine)
        assert _TABLE in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns(_TABLE)}
        assert {
            "workspace_id",
            "user_id",
            "harness_breakdown",
            "model_breakdown",
            "needs_rebuild",
            "total_sessions",
            "last_updated_at",
        } <= columns
        pk = set(inspector.get_pk_constraint(_TABLE)["constrained_columns"])
        assert pk == {"workspace_id", "user_id"}
    finally:
        clear_engine_cache()


def test_downgrade_drops_table(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "zb3c4d5e6f7g")
        assert _TABLE in sa.inspect(engine).get_table_names()
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "mm1a2b3c4d5e")
        assert _TABLE not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
        clear_engine_cache()
