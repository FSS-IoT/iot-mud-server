"""Ticket #88: offline backup, restore, systemd, and the version refusal.

A backup is a consistent SQLite copy taken with the online backup API, so a
committed row still sitting in the WAL is included. A restore replaces the
target database from such a copy, but only after the source proves it is a
current-or-older IoT MUD database; a foreign SQLite file and a newer
`user_version` are refused. Every refusal is an `error:` line, never a
traceback. The documented systemd unit is checked with `systemd-analyze
verify` when that binary exists.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mud.admin import main
from src.mud.auth import register_character
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.db import (
    SCHEMA_VERSION,
    DatabaseTooNew,
    open_database,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT = REPO_ROOT / "deploy" / "iot-mud.service"
OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"

# Every table whose rows a backup must carry. `event_log` is appended to live,
# so it is not part of the equality check; the world, the people, and the keys
# are.
TABLES = (
    "rooms",
    "exits",
    "objects",
    "npcs",
    "world_meta",
    "players",
    "character_keys",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _one(path: Path, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    rows = _query(path, sql, params)
    return rows[0] if rows else None


def _dump(path: Path) -> dict:
    """The schema version and every row a backup must preserve."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "tables": {
                table: [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    )
                ]
                for table in TABLES
            },
        }
    finally:
        conn.close()


def _seed(path: Path) -> None:
    """Seed the world and register one character with a key."""
    assert main(["seed", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        room = SimpleNamespace(
            item_id="gate",
            name="Village Gate",
            description="A heavy stone gate stands tall.",
            dark=False,
        )
        register_character(conn, "Ada", start_room=room)
        with conn:
            conn.execute("UPDATE players SET asleep = 1 WHERE name = 'Ada'")
    finally:
        conn.close()


def _run_admin(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "src.mud.admin", *argv],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Green path: backup, then restore into a fresh path
# ---------------------------------------------------------------------------


def test_backup_then_restore_preserves_the_rows_and_user_version(tmp_path):
    source = tmp_path / "mud.db"
    backup = tmp_path / "backup" / "mud-backup.db"
    restored = tmp_path / "restored" / "mud.db"
    _seed(source)
    expected = _dump(source)

    assert main(["backup", "--db", str(source), "--to", str(backup)]) == 0

    assert backup.is_file()
    assert backup.resolve() != source.resolve()
    assert _dump(backup) == expected
    assert expected["tables"]["players"], "the character must be in the backup"

    assert main(["restore", "--from", str(backup), "--db", str(restored)]) == 0

    assert _dump(restored) == expected
    assert _one(restored, "PRAGMA user_version")[0] == SCHEMA_VERSION


def test_backup_includes_a_committed_row_still_in_the_wal(tmp_path):
    source = tmp_path / "mud.db"
    backup = tmp_path / "mud-backup.db"
    _seed(source)

    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO world_meta(key, value) VALUES ('wal_probe', 'visible')"
        )
        writer.commit()
        wal = source.with_name(source.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0

        # The writer stays open: the copy must read the WAL, not just the main
        # database file.
        assert main(["backup", "--db", str(source), "--to", str(backup)]) == 0
    finally:
        writer.close()

    row = _one(
        backup, "SELECT value FROM world_meta WHERE key = 'wal_probe'"
    )
    assert row["value"] == "visible"


def test_restore_migrates_an_older_backup(tmp_path):
    source = tmp_path / "mud.db"
    backup = tmp_path / "mud-backup.db"
    restored = tmp_path / "restored.db"
    _seed(source)
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()

    assert main(["backup", "--db", str(source), "--to", str(backup)]) == 0
    assert _one(backup, "PRAGMA user_version")[0] == 0

    assert main(["restore", "--from", str(backup), "--db", str(restored)]) == 0

    assert _one(restored, "PRAGMA user_version")[0] == SCHEMA_VERSION
    assert _one(restored, "SELECT COUNT(*) FROM rooms")[0] == 5


def test_backup_and_restore_honor_the_config_storage_path(tmp_path):
    source = tmp_path / "mud.db"
    backup = tmp_path / "mud-backup.db"
    restored = tmp_path / "restored.db"
    _seed(source)

    source_config = tmp_path / "source.toml"
    source_config.write_text(
        f'[storage]\npath = "{source}"\n', encoding="utf-8"
    )
    target_config = tmp_path / "target.toml"
    target_config.write_text(
        f'[storage]\npath = "{restored}"\n', encoding="utf-8"
    )

    assert main(["backup", "--config", str(source_config), "--to", str(backup)]) == 0
    assert main(
        ["restore", "--from", str(backup), "--config", str(target_config)]
    ) == 0

    assert _dump(restored)["tables"]["world_meta"]


# ---------------------------------------------------------------------------
# Refusals: missing, foreign, newer, and an existing target
# ---------------------------------------------------------------------------


def test_backup_refuses_a_missing_database(tmp_path, capsys):
    missing = tmp_path / "never-created.db"
    destination = tmp_path / "backup.db"

    assert main(["backup", "--db", str(missing), "--to", str(destination)]) == 1

    captured = capsys.readouterr()
    assert "database not found" in captured.err
    assert str(missing) in captured.err
    assert not destination.exists()


def test_restore_refuses_a_missing_backup(tmp_path, capsys):
    target = tmp_path / "target.db"

    assert main(
        ["restore", "--from", str(tmp_path / "absent.db"), "--db", str(target)]
    ) == 1

    assert "backup not found" in capsys.readouterr().err
    assert not target.exists()


def test_restore_refuses_a_file_that_is_not_sqlite(tmp_path, capsys):
    source = tmp_path / "notes.txt"
    source.write_text("this is not a database\n", encoding="utf-8")
    target = tmp_path / "target.db"

    assert main(["restore", "--from", str(source), "--db", str(target)]) == 1

    captured = capsys.readouterr()
    assert "not a SQLite database" in captured.err
    assert not target.exists()


def test_restore_refuses_a_foreign_sqlite_database(tmp_path, capsys):
    source = tmp_path / "foreign.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("CREATE TABLE notes(body TEXT)")
        conn.commit()
    finally:
        conn.close()
    target = tmp_path / "target.db"

    assert main(["restore", "--from", str(source), "--db", str(target)]) == 1

    captured = capsys.readouterr()
    assert "not an IoT MUD database" in captured.err
    assert "rooms" in captured.err
    assert not target.exists()


def test_restore_refuses_a_newer_user_version(tmp_path, capsys):
    source = tmp_path / "newer.db"
    _seed(source)
    conn = sqlite3.connect(source)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()
    target = tmp_path / "target.db"

    assert main(["restore", "--from", str(source), "--db", str(target)]) == 1

    captured = capsys.readouterr()
    assert f"schema version {SCHEMA_VERSION + 1}" in captured.err
    assert "newer than this server supports" in captured.err
    assert "upgrade the server or restore an older backup" in captured.err
    assert not target.exists()


def test_restore_into_an_existing_target_requires_yes(tmp_path, capsys):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed(source)
    assert main(["backup", "--db", str(source), "--to", str(backup)]) == 0

    _seed(target)
    conn = sqlite3.connect(target)
    try:
        conn.execute(
            "INSERT INTO world_meta(key, value) VALUES ('sentinel', 'keep-me')"
        )
        conn.commit()
    finally:
        conn.close()
    before = _dump(target)

    assert main(["restore", "--from", str(backup), "--db", str(target)]) == 2

    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "offline" in captured.err
    assert _dump(target) == before

    assert main(
        ["restore", "--from", str(backup), "--db", str(target), "--yes"]
    ) == 0
    assert "sentinel" not in {
        row["key"] for row in _dump(target)["tables"]["world_meta"]
    }


def test_backup_refuses_to_overwrite_a_destination_without_yes(tmp_path, capsys):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    _seed(source)
    assert main(["backup", "--db", str(source), "--to", str(backup)]) == 0
    before = _dump(backup)

    assert main(["backup", "--db", str(source), "--to", str(backup)]) == 2

    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert _dump(backup) == before


def test_backup_refuses_the_source_as_the_destination(tmp_path, capsys):
    source = tmp_path / "mud.db"
    _seed(source)

    assert main(["backup", "--db", str(source), "--to", str(source)]) == 2

    assert "already the source" in capsys.readouterr().err
    assert _one(source, "SELECT COUNT(*) FROM rooms")[0] == 5


# ---------------------------------------------------------------------------
# Bad input is an error line, never a traceback
# ---------------------------------------------------------------------------


def test_bad_input_never_prints_a_traceback(tmp_path):
    text = tmp_path / "text.db"
    text.write_text("not a database\n", encoding="utf-8")
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    try:
        conn.execute("CREATE TABLE notes(body TEXT)")
        conn.commit()
    finally:
        conn.close()
    newer = tmp_path / "newer.db"
    _seed(newer)
    conn = sqlite3.connect(newer)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    cases = [
        ("backup", "--db", str(tmp_path / "absent.db"),
         "--to", str(tmp_path / "out.db")),
        ("restore", "--from", str(tmp_path / "absent.db"),
         "--db", str(tmp_path / "out.db")),
        ("restore", "--from", str(text), "--db", str(tmp_path / "out.db")),
        ("restore", "--from", str(foreign), "--db", str(tmp_path / "out.db")),
        ("restore", "--from", str(newer), "--db", str(tmp_path / "out.db")),
    ]

    for argv in cases:
        result = _run_admin(*argv)
        assert result.returncode != 0, argv
        assert "Traceback" not in result.stderr, (argv, result.stderr)
        assert result.stderr.startswith("error:"), (argv, result.stderr)


# ---------------------------------------------------------------------------
# The unit file and the operations page
# ---------------------------------------------------------------------------


def test_systemd_unit_is_a_generic_template_for_the_state_directory():
    text = UNIT.read_text(encoding="utf-8")

    assert "StateDirectory=iot-mud" in text
    assert "User=iot-mud" in text
    assert "--db /var/lib/iot-mud/mud.db" in text
    assert "Restart=on-failure" in text
    assert "/opt/iot-mud" in text, "the source path must be a documented placeholder"


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze is not installed",
)
def test_systemd_unit_passes_systemd_analyze_verify():
    result = subprocess.run(
        ["systemd-analyze", "verify", str(UNIT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_operations_documents_backup_restore_unit_and_refusal():
    text = OPERATIONS.read_text(encoding="utf-8")

    for needle in (
        "admin backup",
        "admin restore",
        "iot-mud.service",
        "StateDirectory=iot-mud",
        "--db",
        "offline",
        "user_version",
        "newer than this server supports",
        "upgrade the server or restore an older backup",
    ):
        assert needle in text, needle


# ---------------------------------------------------------------------------
# A newer schema refuses at startup, with the documented message
# ---------------------------------------------------------------------------


def test_open_database_refuses_a_newer_user_version(tmp_path):
    path = tmp_path / "future.db"
    _seed(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(DatabaseTooNew) as excinfo:
        open_database(path)

    message = str(excinfo.value)
    assert f"schema version {SCHEMA_VERSION + 1}" in message
    assert "newer than this server supports" in message
    assert "upgrade the server or restore an older backup" in message


@pytest.mark.asyncio
async def test_v2_server_refuses_a_newer_schema_before_binding(tmp_path):
    path = tmp_path / "future.db"
    _seed(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    server = V2Server(Engine(), port=0, db_path=path)
    with pytest.raises(DatabaseTooNew, match="newer than this server supports"):
        await server.run()

    assert server.port == 0
