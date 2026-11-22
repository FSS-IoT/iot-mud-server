"""Ticket #73: `admin rekey` rotates a character's key offline.

The command runs against a seeded database with no server: it issues a new
`mud1-<key id>-<secret>` key, revokes every key the character still holds, and
leaves the durable character alone. The old key is rejected by
`login_character`, the new key logs in, and `PlayerStore.load()` returns the
same room and inventory before and after.

The key is printed to stdout exactly once, never written to the database in
plaintext, and never logged.
"""

from __future__ import annotations

import base64
import logging
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mud.admin import main
from src.mud.auth import (
    AuthError,
    Registration,
    login_character,
    register_character,
)
from src.mud.storage.db import open_database
from src.mud.storage.store import CharacterState, PlayerStore

NAME = "Ada"
KEY_RE = re.compile(r"mud1-k[0-9a-f]{16}-\S+")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(path: Path) -> None:
    assert main(["seed", "--db", str(path)]) == 0


def _room(conn: sqlite3.Connection, room_id: str) -> SimpleNamespace:
    row = conn.execute(
        "SELECT id, name, description, dark FROM rooms WHERE id = ?",
        (room_id,),
    ).fetchone()
    return SimpleNamespace(
        item_id=row["id"],
        name=row["name"],
        description=row["description"],
        dark=bool(row["dark"]),
    )


def _register(path: Path, name: str = NAME) -> Registration:
    conn = open_database(path)
    try:
        return register_character(conn, name, start_room=_room(conn, "gate"))
    finally:
        conn.close()


def _collect_and_move(path: Path, name: str = NAME) -> CharacterState:
    """Give the character a room and an inventory through the real store."""
    conn = open_database(path)
    try:
        store = PlayerStore(conn, name)
        market = _room(conn, "market")
        store.move(room=market)
        store.take(
            SimpleNamespace(
                instance_id="gold_coin",
                name="Gold Coin",
                description="A worn gold coin.",
            ),
            room=market,
        )
        return store.load()
    finally:
        conn.close()


def _printed_key(out: str) -> str:
    keys = KEY_RE.findall(out)
    assert len(keys) == 1, out
    return keys[0]


def _key_id(key: str) -> str:
    return key.split("-", 2)[1]


def _secret_text(key: str) -> str:
    return key.split("-", 2)[2]


def _secret_bytes(key: str) -> bytes:
    secret = _secret_text(key)
    padded = secret + "=" * (-len(secret) % 4)
    return base64.urlsafe_b64decode(padded)


def _key_rows(path: Path) -> list[dict]:
    conn = open_database(path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT key_id, player_id, salt, hash, created_at, revoked_at "
                "FROM character_keys ORDER BY key_id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _player_rows(path: Path) -> list[dict]:
    conn = open_database(path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id, name, room_id, created_at, last_seen, asleep "
                "FROM players ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _database_bytes(path: Path) -> bytes:
    blob = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    return blob


# ---------------------------------------------------------------------------
# The rotation itself
# ---------------------------------------------------------------------------


def test_rekey_issues_a_new_key_revokes_the_old_and_keeps_the_character(
        tmp_path, capsys, caplog):
    path = tmp_path / "mud.db"
    _seed(path)
    registration = _register(path)
    before = _collect_and_move(path)
    players_before = _player_rows(path)
    capsys.readouterr()
    caplog.set_level(logging.DEBUG, logger="src.mud")

    assert main(["rekey", NAME, "--db", str(path)]) == 0

    out, err = capsys.readouterr()
    new_key = _printed_key(out)

    # Same layout, fresh entropy, and a new public id.
    assert new_key.startswith("mud1-")
    assert new_key != registration.key
    assert _key_id(new_key) != registration.key_id
    assert len(_secret_bytes(new_key)) == 32
    assert len(_secret_bytes(registration.key)) == 32

    # Printed once, on stdout, and the old key is not echoed at all.
    assert out.count(new_key) == 1
    assert new_key not in err
    assert registration.key not in out + err
    assert new_key not in caplog.text
    assert _secret_text(new_key) not in caplog.text

    # The key is a credential, not a stored value.
    assert new_key.encode() not in _database_bytes(path)

    conn = open_database(path)
    try:
        # The old key is dead; the new one finds the same character.
        with pytest.raises(AuthError) as old:
            login_character(conn, registration.key)
        assert old.value.code == "invalid_key"

        login = login_character(conn, new_key)
        assert login.name == NAME
        assert login.player_id == registration.player_id
        assert login.key_id == _key_id(new_key)

        # Room and inventory are exactly what the store had before.
        assert PlayerStore(conn, NAME).load() == before
        assert before.room_id == "market"
        assert before.items == ("gold_coin",)

        old_row = conn.execute(
            "SELECT revoked_at FROM character_keys WHERE key_id = ?",
            (registration.key_id,),
        ).fetchone()
        assert old_row["revoked_at"] is not None

        new_row = conn.execute(
            "SELECT player_id, revoked_at, salt, hash FROM character_keys "
            "WHERE key_id = ?",
            (_key_id(new_key),),
        ).fetchone()
        assert new_row["player_id"] == registration.player_id
        assert new_row["revoked_at"] is None
        assert len(bytes.fromhex(new_row["salt"])) == 16
        assert new_row["hash"].startswith("scrypt$")

        assert conn.execute(
            "SELECT COUNT(*) FROM character_keys WHERE player_id = ?",
            (registration.player_id,),
        ).fetchone()[0] == 2
    finally:
        conn.close()

    # Nothing about the character itself moved.
    assert _player_rows(path) == players_before


def test_rekey_revokes_every_live_key_and_only_the_newest_logs_in(
        tmp_path, capsys):
    path = tmp_path / "mud.db"
    _seed(path)
    registration = _register(path)
    capsys.readouterr()

    assert main(["rekey", NAME, "--db", str(path)]) == 0
    first_new = _printed_key(capsys.readouterr().out)

    assert main(["rekey", NAME, "--db", str(path)]) == 0
    second_new = _printed_key(capsys.readouterr().out)

    assert len({registration.key, first_new, second_new}) == 3

    conn = open_database(path)
    try:
        for dead in (registration.key, first_new):
            with pytest.raises(AuthError) as error:
                login_character(conn, dead)
            assert error.value.code == "invalid_key"
        assert login_character(conn, second_new).name == NAME

        active = conn.execute(
            "SELECT key_id FROM character_keys "
            "WHERE player_id = ? AND revoked_at IS NULL",
            (registration.player_id,),
        ).fetchall()
        assert [row["key_id"] for row in active] == [_key_id(second_new)]

        assert conn.execute(
            "SELECT COUNT(*) FROM character_keys WHERE player_id = ?",
            (registration.player_id,),
        ).fetchone()[0] == 3
    finally:
        conn.close()


def test_rekey_recovers_a_character_whose_keys_were_all_revoked(
        tmp_path, capsys):
    path = tmp_path / "mud.db"
    _seed(path)
    registration = _register(path)

    conn = open_database(path)
    try:
        with conn:
            conn.execute(
                "UPDATE character_keys SET revoked_at = ? WHERE player_id = ?",
                ("2026-01-01T00:00:00Z", registration.player_id),
            )
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["rekey", NAME, "--db", str(path)]) == 0

    out, err = capsys.readouterr()
    new_key = _printed_key(out)
    assert "revoked 0 keys" in err

    conn = open_database(path)
    try:
        assert login_character(conn, new_key).name == NAME
    finally:
        conn.close()


def test_a_failed_insert_rolls_the_revocation_back(tmp_path, capsys):
    """Revoke and insert are one transaction, so half a rekey never lands."""
    path = tmp_path / "mud.db"
    _seed(path)
    registration = _register(path)

    conn = open_database(path)
    try:
        with conn:
            conn.execute(
                "CREATE TRIGGER refuse_new_key BEFORE INSERT ON character_keys "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["rekey", NAME, "--db", str(path)]) == 1

    out, err = capsys.readouterr()
    assert KEY_RE.findall(out + err) == []
    assert "refused" in err or "could not" in err

    conn = open_database(path)
    try:
        # The old key is still live and no partial new row exists.
        assert login_character(conn, registration.key).name == NAME
        rows = conn.execute(
            "SELECT key_id FROM character_keys WHERE player_id = ?",
            (registration.player_id,),
        ).fetchall()
        assert [row["key_id"] for row in rows] == [registration.key_id]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Lookup failures: only the request is mentioned
# ---------------------------------------------------------------------------


def test_unknown_name_changes_nothing_and_names_only_the_request(
        tmp_path, capsys):
    path = tmp_path / "mud.db"
    _seed(path)
    _register(path, "Ada")
    _register(path, "Ben")
    keys_before = _key_rows(path)
    players_before = _player_rows(path)
    capsys.readouterr()

    assert main(["rekey", "Nobody", "--db", str(path)]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "Nobody" in err
    # No other character is named, and no key-shaped text is printed.
    assert "Ada" not in err
    assert "Ben" not in err
    assert KEY_RE.findall(out + err) == []

    assert _key_rows(path) == keys_before
    assert _player_rows(path) == players_before


def test_name_lookup_is_case_insensitive_and_keeps_the_stored_casing(
        tmp_path, capsys):
    path = tmp_path / "mud.db"
    _seed(path)
    _register(path, "Ada")
    capsys.readouterr()

    assert main(["rekey", "ada", "--db", str(path)]) == 0

    out, _err = capsys.readouterr()
    assert out.startswith("rekeyed Ada: ")
    new_key = _printed_key(out)

    conn = open_database(path)
    try:
        assert login_character(conn, new_key).name == "Ada"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Missing database, world, or player
# ---------------------------------------------------------------------------


def test_missing_database_is_refused_without_creating_it(tmp_path, capsys):
    path = tmp_path / "absent.db"

    assert main(["rekey", NAME, "--db", str(path)]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "database not found" in err
    assert not path.exists()


def test_database_without_a_world_is_refused(tmp_path, capsys):
    path = tmp_path / "empty.db"
    assert main(["init-db", "--db", str(path)]) == 0
    capsys.readouterr()

    assert main(["rekey", NAME, "--db", str(path)]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "no world" in err
    assert KEY_RE.findall(out + err) == []
    # Nothing was created on the way to the refusal.
    assert _player_rows(path) == []
    assert _key_rows(path) == []


def test_a_file_that_is_not_a_database_is_refused(tmp_path, capsys):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a database", encoding="utf-8")

    assert main(["rekey", NAME, "--db", str(path)]) == 1

    _out, err = capsys.readouterr()
    assert "could not open" in err
    assert path.read_text(encoding="utf-8") == "this is not a database"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_rekey_uses_the_config_storage_path(tmp_path, capsys):
    path = tmp_path / "from-config.db"
    config = tmp_path / "mud.toml"
    config.write_text(f'[storage]\npath = "{path}"\n', encoding="utf-8")

    assert main(["seed", "--config", str(config)]) == 0
    _register(path)
    capsys.readouterr()

    assert main(["rekey", NAME, "--config", str(config)]) == 0

    new_key = _printed_key(capsys.readouterr().out)
    conn = open_database(path)
    try:
        assert login_character(conn, new_key).name == NAME
    finally:
        conn.close()
