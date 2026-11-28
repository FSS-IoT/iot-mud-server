"""Ticket #183: fire-making skill, ignition tools, and fuel.

`light` on an unlit source needs a character who knows fire making, an ignition
tool, and fuel. A lit source already equipped can pass the flame without tools.
The chance is deterministic from the engine RNG for a given seed and player.

Red on the parent: `Player` has no skills, `PlayerStore` has no skill column,
and `light` still ignites any carried source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.mud.content import Entity, Room
from src.mud.content.models import GameObject
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.storage.db import SCHEMA_VERSION, connect, migrate, open_database
from src.mud.storage.store import PlayerStore
from src.mud.world.loader import load_world
from src.mud.world.seed import seed_world
from tests.golden.harness import HarnessClient, server_harness

WORLD = """
name: Fire Test
start_room: hall
rng_seed: 7
rooms:
  - id: hall
    name: Hall
    description: A stone hall.
    objects:
      - id: torch
        name: Torch
        description: A wooden torch.
        weight: 0.5
        light_source: true
        equip_slots: [hand]
        hands: 1
      - id: flint
        name: Flint and Steel
        description: A striker and a shard of flint.
        weight: 0.1
        ignition: true
        fire_bonus: 0.2
      - id: kindling
        name: Kindling
        description: A bundle of dry twigs.
        weight: 0.2
        fuel: true
        fire_bonus: 0.1
"""


def _world_file(tmp_path: Path) -> Path:
    path = tmp_path / "world.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


def _engine(skill: int = 1):
    engine = Engine()
    room = Room("Camp", "A dark camp.", dark=True)
    room.item_id = "camp"
    engine._rooms["Camp"] = room
    engine.start_room = room
    ada = engine.register_player("Ada")
    ada.skills["firemaking"] = skill
    torch = Entity(
        "Torch", "A wooden torch.", light_source=True,
        equip_slots=("hand",), hands=1, item_id="torch")
    lantern = Entity(
        "Lantern", "A brass lantern.", light_source=True,
        equip_slots=("hand", "belt"), hands=1, item_id="lantern")
    flint = Entity(
        "Flint and Steel", "A striker.", ignition=True, fire_bonus=0.2,
        item_id="flint")
    kindling = Entity(
        "Kindling", "Dry twigs.", fuel=True, fire_bonus=0.1,
        item_id="kindling")
    for item in (torch, lantern, flint, kindling):
        item.instance_id = item.item_id
        ada.add_content(item)
    return engine, ada, torch, lantern, flint, kindling


async def _code(engine, command: str) -> str | None:
    result = await execute(engine, "Ada", command)
    error = next(
        (event for event in result.events if event.type == "error"), None)
    return error.data.get("code") if error is not None else None


async def test_no_skill_refuses_to_make_fire():
    engine, _ada, torch, _lantern, _flint, _kindling = _engine(skill=0)

    assert await _code(engine, "light torch") == "no_skill"
    assert torch.lit is False


async def test_no_ignition_tool_is_a_typed_error():
    engine, ada, torch, _lantern, flint, _kindling = _engine()
    ada.remove_content(flint)

    assert await _code(engine, "light torch") == "no_ignition"
    assert torch.lit is False


async def test_no_fuel_is_a_typed_error():
    engine, ada, torch, _lantern, _flint, kindling = _engine()
    ada.remove_content(kindling)

    assert await _code(engine, "light torch") == "no_fuel"
    assert torch.lit is False


async def test_a_successful_attempt_lights_the_source():
    engine, _ada, torch, _lantern, _flint, _kindling = _engine()
    engine.fire_chance = lambda *args: 1.0

    result = await execute(engine, "Ada", "light torch")

    assert torch.lit is True
    assert {event.type for event in result.events} == {"object.lit"}


async def test_a_failed_attempt_is_a_notice_and_keeps_the_items():
    engine, ada, torch, _lantern, flint, kindling = _engine()
    engine.fire_chance = lambda *args: 0.0

    result = await execute(engine, "Ada", "light torch")

    assert [(event.type,) for event in result.events] == [("notice",)]
    assert torch.lit is False
    assert flint in ada.contents
    assert kindling in ada.contents


async def test_an_equipped_lit_source_passes_the_flame_without_tools():
    engine, ada, _torch, lantern, flint, kindling = _engine(skill=0)
    torch = next(item for item in ada.contents if item.item_id == "torch")
    torch.lit = True
    ada.equip(torch, "hand_1")
    ada.remove_content(flint)
    ada.remove_content(kindling)
    engine.fire_chance = lambda *args: (_ for _ in ()).throw(
        AssertionError("flame transfer must not roll"))

    await engine.process_command("light lantern", "Ada")

    assert lantern.lit is True
    assert lantern.equipped_slot in ("hand_1", "hand_2")


async def test_skill_survives_a_restart(tmp_path):
    conn = open_database(tmp_path / "mud.db")
    hall = Room("Hall", "A stone hall.")
    hall.item_id = "hall"
    try:
        store = PlayerStore(conn, "Ada")
        store.move(room=hall)
        store.set_skill("firemaking", 1)

        state = PlayerStore(conn, "Ada").load()
        assert state.skills == {"firemaking": 1}

        engine = Engine()
        engine._rooms[hall.name] = hall
        engine.start_room = hall
        player = engine.register_player("Ada")
        engine.restore_player(
            player, room=hall, item_keys=(), skills=state.skills)
        assert player.skills == {"firemaking": 1}
    finally:
        conn.close()


def test_schema_1_migrates_to_the_skill_column(tmp_path):
    conn = connect(tmp_path / "old.db")
    try:
        conn.execute(
            "CREATE TABLE players (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "room_id TEXT, created_at TEXT NOT NULL, last_seen TEXT, "
            "asleep INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO players(name, created_at) VALUES ('Ada', 'now')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        migrate(conn, tmp_path / "old.db")

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute(
            "SELECT skills FROM players WHERE name = 'Ada'").fetchone()
        assert row["skills"] == "{}"
    finally:
        conn.close()


async def test_firemaking_items_are_in_the_product_world(tmp_path):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    conn = open_database(db_path)
    try:
        seed_world(conn, world_path, epoch="fire")
    finally:
        conn.close()

    engine = Engine()
    await load_world(engine, world_path)
    async with server_harness(engine, db_path=db_path) as server, \
            HarnessClient(server) as ada:
        await ada.register_key("Ada")
        server.engine.get_player("Ada").skills["firemaking"] = 1
        await ada.input("take torch", 2)
        await ada.next("object.taken", msg_id=2)
        await ada.input("take flint", 3)
        await ada.next("object.taken", msg_id=3)
        await ada.input("take kindling", 4)
        await ada.next("object.taken", msg_id=4)
        engine.fire_chance = lambda *args: 1.0
        await ada.input("light torch", 5)
        await ada.next("object.lit")


def test_content_model_accepts_ignition_and_fuel():
    flint = GameObject(
        id="flint", name="Flint", ignition=True, fire_bonus=0.2)
    kindling = GameObject(id="kindling", name="Kindling", fuel=True)
    assert flint.ignition is True
    assert flint.fire_bonus == 0.2
    assert kindling.fuel is True

    with pytest.raises(ValidationError):
        GameObject(id="bad", name="Bad", fire_bonus=-1.0)
