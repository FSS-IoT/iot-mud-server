"""Ticket #62: `admin init-db`."""

import sqlite3
import subprocess
import sys
from pathlib import Path

from src.mud.admin import main
from src.mud.storage.db import SCHEMA_VERSION


def test_init_db_creates_the_schema(tmp_path):
    path = tmp_path / "mud.db"

    assert main(["init-db", "--db", str(path)]) == 0

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_is_idempotent_and_keeps_data(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0

    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO world_meta(key, value) VALUES ('name', 'Kept')")
        conn.commit()
    finally:
        conn.close()

    assert main(["init-db", "--db", str(path)]) == 0

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM world_meta WHERE key = 'name'"
        ).fetchone()
        assert row[0] == "Kept"
    finally:
        conn.close()


def test_init_db_uses_the_config_storage_path(tmp_path):
    config = tmp_path / "mud.toml"
    config.write_text(
        f'[storage]\npath = "{tmp_path / "from-config.db"}"\n',
        encoding="utf-8",
    )

    assert main(["init-db", "--config", str(config)]) == 0

    assert (tmp_path / "from-config.db").exists()


def test_module_entrypoint_works(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    path = tmp_path / "cli.db"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.mud.admin",
            "init-db",
            "--db",
            str(path),
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert path.exists()
