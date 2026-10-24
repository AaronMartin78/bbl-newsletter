"""DuckDB access: schema initialization and registry sync.

Single writer by design: only the pipeline process on HUB opens the
database for writing.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .registry import Registry


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path))


def init_schema(con: duckdb.DuckDBPyConnection, schema_path: str | Path) -> None:
    """Execute schema.sql (idempotent: CREATE TABLE IF NOT EXISTS throughout)."""
    con.execute(Path(schema_path).read_text(encoding="utf-8"))


def sync_sources(con: duckdb.DuckDBPyConnection, registry: Registry) -> None:
    """Mirror the registry into the sources table and ensure source_state rows.

    Rows are upserted; sources removed from the registry are kept (they are
    referenced by collected articles) but no longer polled.
    """
    for s in registry.sources:
        con.execute(
            """
            INSERT INTO sources (source_id, publisher, type, url, registry_version)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                publisher = excluded.publisher,
                type = excluded.type,
                url = excluded.url,
                registry_version = excluded.registry_version
            """,
            [s.id, s.publisher, s.type, s.url, registry.version],
        )
        con.execute(
            """
            INSERT INTO source_state (source_id)
            SELECT ? WHERE NOT EXISTS
                (SELECT 1 FROM source_state WHERE source_id = ?)
            """,
            [s.id, s.id],
        )
