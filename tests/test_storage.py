"""Ticket #62: SQLite schema, migrations, and database reuse."""

import asyncio
import sqlite3

import pytest

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server, parse_args
from src.mud.storage.db import (
    SCHEMA_VERSION,
    DatabaseTooNew,
    connect,
    migrate,
    open_database,
)

TABLES = {
    "world_meta",
    "players",
    "character_keys",
    "rooms",
    "exits",
    "objects",
    "npcs",
    "event_log",
}

INDEXES = {
    "idx_character_keys_player",
    "idx_players_room",
    "idx_objects_room",
    "idx_objects_owner",
    "idx_objects_container",
    "idx_npcs_room",
    "idx_event_log_room",
}


def _schema_names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = ? AND name NOT LIKE 'sqlite_%'",
        (kind,),
    ).fetchall()
    return {row[0] for row in rows}


def test_connect_then_migrate_sets_the_schema_version(tmp_path):
    path = tmp_path / "fresh.db"

    conn = connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert TABLES <= _schema_names(conn, "table")
    finally:
        conn.close()


def test_fresh_database_has_schema_and_pragmas(tmp_path):
    path = tmp_path / "mud.db"

    conn = open_database(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        assert TABLES <= _schema_names(conn, "table")
        assert INDEXES <= _schema_names(conn, "index")

        with conn:
            conn.execute(
                "INSERT INTO world_meta(key, value) VALUES ('name', 'Test')"
            )
    finally:
        conn.close()


def test_second_open_reuses_the_database_and_its_data(tmp_path):
    path = tmp_path / "mud.db"

    first = open_database(path)
    try:
        with first:
            first.execute(
                "INSERT INTO world_meta(key, value) VALUES ('epoch', 'one')"
            )
    finally:
        first.close()

    second = open_database(path)
    try:
        assert second.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = second.execute(
            "SELECT value FROM world_meta WHERE key = 'epoch'"
        ).fetchone()
        assert row[0] == "one"
    finally:
        second.close()


def test_server_args_accept_db_and_config(tmp_path):
    args = parse_args([
        "--db", str(tmp_path / "server.db"),
        "--config", str(tmp_path / "mud.toml"),
    ])

    assert args.db == tmp_path / "server.db"
    assert args.config == tmp_path / "mud.toml"


def test_newer_schema_refuses_to_open(tmp_path):
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(DatabaseTooNew, match="newer"):
        open_database(path)


async def _wait_for_bind(server: V2Server) -> None:
    for _ in range(200):
        if server.port:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("v2 server did not bind")


@pytest.mark.asyncio
async def test_server_starts_against_the_database_and_reuses_it(tmp_path):
    path = tmp_path / "server.db"

    first = V2Server(Engine(), port=0, db_path=path)
    task = asyncio.create_task(first.run())
    try:
        await _wait_for_bind(first)
        assert path.exists()
        assert first.db is not None
    finally:
        first.stop()
        await asyncio.wait_for(task, timeout=3.0)

    sentinel = open_database(path)
    try:
        with sentinel:
            sentinel.execute(
                "INSERT INTO world_meta(key, value) VALUES ('epoch', 'kept')"
            )
    finally:
        sentinel.close()

    second = V2Server(Engine(), port=0, db_path=path)
    task = asyncio.create_task(second.run())
    try:
        await _wait_for_bind(second)
    finally:
        second.stop()
        await asyncio.wait_for(task, timeout=3.0)

    reopened = open_database(path)
    try:
        row = reopened.execute(
            "SELECT value FROM world_meta WHERE key = 'epoch'"
        ).fetchone()
        assert row[0] == "kept"
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_server_refuses_a_newer_schema_before_binding(tmp_path):
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
    finally:
        raw.close()

    server = V2Server(Engine(), port=0, db_path=path)
    with pytest.raises(DatabaseTooNew, match="newer"):
        await server.run()

    assert server.port == 0
