"""Ticket #149: per-character base carry capacity.

Carry capacity is a durable per-character base (`players.capacity_base`,
default `30.0`) plus a modifier layer that is empty for now. Strength and item
modifiers come later; this ticket adds only the base and the seam.

The wire keeps the computed `capacity` field and does not grow a base field, so
a client sees one number and the server derives it from the character.

These tests are written before the behavior exists, so they fail now (RED).
"""

from __future__ import annotations

from pathlib import Path

from src.mud.admin import main
from src.mud.content import DEFAULT_CARRY_CAPACITY, Entity, Room
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.storage.db import SCHEMA_VERSION, connect, migrate, open_database
from src.mud.storage.store import PlayerStore

REPO_ROOT = Path(__file__).resolve().parents[1]

WORLD = """
name: Capacity Test
start_room: hall
rng_seed: 7
rooms:
  - id: hall
    name: Hall
    description: A stone hall.
"""


def _world_file(tmp_path: Path) -> Path:
    path = tmp_path / "world.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


def _insert_player(conn, name: str) -> None:
    conn.execute(
        "INSERT INTO players(name, created_at) VALUES (?, 'now')", (name,))


async def _engine(tmp_path: Path) -> Engine:
    from src.mud.world.loader import load_world

    engine = Engine()
    await load_world(engine, _world_file(tmp_path))
    return engine


async def _admission(tmp_path: Path):
    from src.mud.admission import CharacterAdmission

    engine = await _engine(tmp_path)
    conn = open_database(tmp_path / "mud.db")
    admission = CharacterAdmission(engine, conn)
    return engine, conn, admission


# --- the durable base -------------------------------------------------------


def test_two_characters_can_have_different_bases(tmp_path):
    conn = open_database(tmp_path / "mud.db")
    try:
        _insert_player(conn, "Ada")
        _insert_player(conn, "Ben")
        conn.commit()

        PlayerStore(conn, "Ada").set_capacity_base(40.0)
        PlayerStore(conn, "Ben").set_capacity_base(15.0)

        assert PlayerStore(conn, "Ada").capacity_base() == 40.0
        assert PlayerStore(conn, "Ben").capacity_base() == 15.0
    finally:
        conn.close()


async def test_a_set_base_survives_a_restart(tmp_path):
    engine, conn, admission = await _admission(tmp_path)
    key = admission.register("Ada").registration.key
    PlayerStore(conn, "Ada").set_capacity_base(45.0)
    conn.close()

    conn = open_database(tmp_path / "mud.db")
    engine = await _engine(tmp_path)
    from src.mud.admission import CharacterAdmission

    result = CharacterAdmission(engine, conn).resume(key)

    assert result.player.capacity_base == 45.0
    conn.close()


async def test_a_new_character_has_the_default_base(tmp_path):
    _, conn, admission = await _admission(tmp_path)

    result = admission.register("Ada")

    assert result.player.capacity_base == DEFAULT_CARRY_CAPACITY
    assert PlayerStore(conn, "Ada").capacity_base() == DEFAULT_CARRY_CAPACITY
    conn.close()


def test_a_non_positive_base_means_no_limit():
    engine = Engine()
    room = Room("Market", "A busy market.")
    room.item_id = "market"
    engine._rooms[room.name] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    boulder = Entity("Boulder", "A granite boulder.", weight=999.0,
                     item_id="boulder")

    player.capacity_base = 0.0
    assert engine.can_carry(player, boulder) is True

    player.capacity_base = -5.0
    assert engine.can_carry(player, boulder) is True


def test_a_schema_2_database_migrates_to_3_and_keeps_rows(tmp_path):
    conn = connect(tmp_path / "old.db")
    try:
        conn.execute(
            "CREATE TABLE players (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "room_id TEXT, created_at TEXT NOT NULL, last_seen TEXT, "
            "asleep INTEGER NOT NULL DEFAULT 0, "
            "skills TEXT NOT NULL DEFAULT '{}')"
        )
        conn.execute(
            "INSERT INTO players(name, created_at) VALUES ('Ada', 'now')")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

        migrate(conn, tmp_path / "old.db")

        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            SCHEMA_VERSION
        row = conn.execute(
            "SELECT name, capacity_base FROM players WHERE name = 'Ada'"
        ).fetchone()
        assert row["name"] == "Ada"
        assert row["capacity_base"] == DEFAULT_CARRY_CAPACITY
    finally:
        conn.close()


# --- the admin command ------------------------------------------------------


def test_admin_capacity_sets_and_persists_the_base(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0

    conn = open_database(path)
    try:
        _insert_player(conn, "Ada")
        conn.commit()
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["capacity", "Ada", "45.5", "--db", str(path)]) == 0

    out, _err = capsys.readouterr()
    assert "Ada" in out
    assert "45.5" in out

    conn = open_database(path)
    try:
        assert PlayerStore(conn, "Ada").capacity_base() == 45.5
    finally:
        conn.close()


def test_admin_capacity_refuses_a_missing_character(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    capsys.readouterr()

    assert main(["capacity", "Nobody", "45.5", "--db", str(path)]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "Nobody" in err


# --- the wire ---------------------------------------------------------------


async def test_inventory_wire_capacity_matches_the_computed_base():
    engine = Engine()
    room = Room("Market", "A busy market.")
    room.item_id = "market"
    engine._rooms[room.name] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    player.capacity_base = 42.5

    result = await execute(engine, "Ada", "inventory")

    payload = result.events[0].data
    assert payload["capacity"] == 42.5
    assert engine.carry_capacity(player) == 42.5
