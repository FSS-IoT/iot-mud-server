"""Open the SQLite database, apply the schema, and refuse newer versions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 2
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class DatabaseError(RuntimeError):
    """The database cannot be opened, migrated, or written."""


class DatabaseTooNew(DatabaseError):
    """The database was written by a newer server."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open one connection with the pragmas every writer needs."""
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _apply_initial_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


def _add_player_skills(conn: sqlite3.Connection) -> None:
    """Schema 2: per-character skills JSON (#183)."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(players)")
    }
    if "skills" not in columns:
        conn.execute(
            "ALTER TABLE players ADD COLUMN skills TEXT NOT NULL DEFAULT '{}'")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _apply_initial_schema,
    1: _add_player_skills,
}


def migrate(conn: sqlite3.Connection, path: str | Path = "<database>") -> None:
    """Bring the schema to `SCHEMA_VERSION`, one `user_version` step at a time."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise DatabaseTooNew(
            f"{path} has schema version {version}, newer than this server "
            f"supports ({SCHEMA_VERSION}); upgrade the server or restore an "
            "older backup"
        )

    while version < SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise DatabaseError(
                f"no migration from schema version {version} to "
                f"{SCHEMA_VERSION} for {path}"
            )
        migration(conn)
        version += 1
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


def open_database(path: str | Path) -> sqlite3.Connection:
    """Connect and migrate; the caller owns the connection."""
    conn = connect(path)
    try:
        migrate(conn, path)
    except Exception:
        conn.close()
        raise
    return conn
