"""Red-green: locked exits and keys (SPEC v2.1.2 criterion 3, #77).

A locked exit refuses movement until the matching key is used. The lock and the
key are declared in content: `exits.<direction>.locked` plus `key` naming the
key object's `id`. `use <key> on <exit>` turns the key, unlocks the door, and
the wire carries `exit.unlocked` to the actor and the room. A wrong or missing
key leaves the door locked. A `one_way` lock has no reciprocal side.

The product world exercises the feature: the Old Tavern's door is locked and
the existing gold key opens it. The YAML world and the legacy JSON oracle must
agree, and the seeder must store the lock on both sides of the door.

These tests are written before the engine carries exit locks, so they fail now
(RED). The validator cases are pinned here because #65 already grew that rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.mud.content import Entity
from src.mud.content.models import Exit
from src.mud.content.models import load_world as load_content_world
from src.mud.content.validator import check_world
from src.mud.content.world import compile_world
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
JSON_WORLD = REPO_ROOT / "data" / "worlds" / "starting_village.json"
YAML_WORLD = REPO_ROOT / "data" / "worlds" / "starting_village.yaml"

LOCKED_WORLD = """
name: Locked Halls
start_room: hall
rng_seed: 77
rooms:
  - id: hall
    name: Hall
    description: A cold hall with a heavy door east.
    exits:
      east:
        to: vault
        locked: true
        key: key_gold_1
        label: iron door
    objects:
      - id: key_gold_1
        name: Gold Key
        description: A small gold key.
  - id: vault
    name: Vault
    description: A dusty vault.
"""

ONE_WAY_WORLD = """
name: One Way
start_room: hall
rng_seed: 78
rooms:
  - id: hall
    name: Hall
    description: A hall with a one-way door east.
    exits:
      east:
        to: vault
        locked: true
        key: key_gold_1
        one_way: true
    objects:
      - id: key_gold_1
        name: Gold Key
        description: A small gold key.
  - id: vault
    name: Vault
    description: A dusty vault.
"""


async def _engine(tmp_path: Path, text: str) -> Engine:
    path = tmp_path / "world.yaml"
    path.write_text(text, encoding="utf-8")
    engine = Engine()
    await load_world(engine, path)
    return engine


def _take(engine: Engine, player_name: str, item_id: str) -> Entity:
    """Move the named world object into the player's inventory."""
    player = engine.get_player(player_name)
    assert player is not None
    for room in engine.rooms.values():
        for item in list(room.contents):
            if getattr(item, "item_id", None) == item_id:
                room.remove_content(item)
                player.add_content(item)
                return item
    raise AssertionError(f"no object {item_id!r} in the world")


def _room(engine: Engine, room_id: str):
    return next(room for room in engine.rooms.values() if room.item_id == room_id)


# --- engine: the door refuses, the key opens ---------------------------------


@pytest.mark.asyncio
async def test_locked_exit_refuses_movement(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")

    moved, result = await engine.move_player("Ada", "east")

    assert moved is False
    assert "locked" in result.lower()
    assert engine.get_player("Ada").location.name == "Hall"


@pytest.mark.asyncio
async def test_matching_key_unlocks_and_then_movement_succeeds(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")
    _take(engine, "Ada", "key_gold_1")

    reply = await engine.process_command("use gold key on iron door", "Ada")

    assert "unlock" in reply.lower()
    moved, result = await engine.move_player("Ada", "east")
    assert moved is True
    assert result == "Vault"


@pytest.mark.asyncio
async def test_use_key_names_the_exit_by_destination_room(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")
    _take(engine, "Ada", "key_gold_1")

    reply = await engine.process_command("use gold key on vault", "Ada")

    assert "unlock" in reply.lower()
    moved, _ = await engine.move_player("Ada", "east")
    assert moved is True


@pytest.mark.asyncio
async def test_wrong_key_keeps_the_exit_locked(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")
    player = engine.get_player("Ada")
    player.add_content(
        Entity("Brass Key", "The wrong key.", item_id="key_brass_9")
    )

    reply = await engine.process_command("use brass key on east", "Ada")

    assert "does not fit" in reply.lower()
    moved, _ = await engine.move_player("Ada", "east")
    assert moved is False


@pytest.mark.asyncio
async def test_missing_key_keeps_the_exit_locked(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")

    reply = await engine.process_command("use gold key on east", "Ada")

    assert "don't have" in reply.lower()
    moved, _ = await engine.move_player("Ada", "east")
    assert moved is False


@pytest.mark.asyncio
async def test_the_key_unlocks_both_sides_of_a_two_way_door(tmp_path):
    """A physical door unlocks once; the reciprocal side comes along."""
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")
    _take(engine, "Ada", "key_gold_1")

    reply = await engine.process_command("use gold key on east", "Ada")
    assert "unlock" in reply.lower()
    moved, _ = await engine.move_player("Ada", "east")
    assert moved is True

    # No second `use`: the same door, seen from the vault side, is unlocked.
    moved, result = await engine.move_player("Ada", "west")
    assert moved is True
    assert result == "Hall"


@pytest.mark.asyncio
async def test_a_one_way_lock_unlocks_and_has_no_reciprocal(tmp_path):
    engine = await _engine(tmp_path, ONE_WAY_WORLD)
    engine.register_player("Ada")
    _take(engine, "Ada", "key_gold_1")

    moved, _ = await engine.move_player("Ada", "east")
    assert moved is False

    await engine.process_command("use gold key on east", "Ada")
    moved, result = await engine.move_player("Ada", "east")

    assert moved is True
    assert result == "Vault"
    assert "west" not in engine.rooms["Vault"].exits


# --- protocol adapter: room state, typed errors, exit events -----------------


@pytest.mark.asyncio
async def test_room_state_reports_locked_and_the_authored_label(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")

    result = await execute(engine, "Ada", "look")

    exits = result.events[0].data["room"]["exits"]
    east = next(entry for entry in exits if entry["direction"] == "east")
    assert east["locked"] is True
    assert east["label"] == "iron door"


@pytest.mark.asyncio
async def test_go_locked_is_error_locked_and_notifies_the_room(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")
    engine.register_player("Ben")

    result = await execute(engine, "Ada", "go east")

    error = next(event for event in result.events if event.type == "error")
    assert error.data["code"] == "locked"
    assert "locked" in error.data["message"].lower()

    locked = next(event for event in result.events if event.type == "exit.locked")
    assert locked.target == "room"
    assert locked.data["exit"]["direction"] == "east"
    assert locked.data["by"] == "Ada"
    assert locked.data["self"] is False
    assert engine.get_player("Ada").location.name == "Hall"


@pytest.mark.asyncio
async def test_use_key_emits_exit_unlocked_to_actor_and_room(tmp_path):
    engine = await _engine(tmp_path, LOCKED_WORLD)
    engine.register_player("Ada")
    engine.register_player("Ben")
    _take(engine, "Ada", "key_gold_1")

    result = await execute(engine, "Ada", "use gold key on east")

    actor = next(
        event for event in result.events
        if event.type == "exit.unlocked" and event.target == "actor"
    )
    room = next(
        event for event in result.events
        if event.type == "exit.unlocked" and event.target == "room"
    )
    assert actor.data["self"] is True
    assert actor.data["by"] == "Ada"
    assert actor.data["exit"] == {"direction": "east", "to": "vault"}
    assert room.data["self"] is False
    assert "unlock" in actor.data["text"].lower()

    moved, _ = await engine.move_player("Ada", "east")
    assert moved is True


# --- product content: the tavern door and the gold key -----------------------


@pytest.mark.parametrize("world_path", [YAML_WORLD, JSON_WORLD], ids=["yaml", "json"])
@pytest.mark.asyncio
async def test_product_world_locked_tavern_door_opens_with_the_gold_key(
    world_path,
):
    engine = Engine()
    await load_world(engine, world_path)
    engine.register_player("Ada")

    await engine.process_command("go north", "Ada")   # gate -> market
    await engine.process_command("go west", "Ada")    # market -> shop
    await engine.process_command("take gold key", "Ada")
    await engine.process_command("go east", "Ada")    # shop -> market

    moved, refusal = await engine.move_player("Ada", "east")
    assert moved is False
    assert "locked" in refusal.lower()

    reply = await engine.process_command("use gold key on tavern door", "Ada")
    assert "unlock" in reply.lower()

    moved, result = await engine.move_player("Ada", "east")
    assert moved is True
    assert result == "Old Tavern"

    # The same door from inside the tavern, with no second unlock.
    moved, result = await engine.move_player("Ada", "west")
    assert moved is True
    assert result == "Marketplace"


def test_product_yaml_declares_the_lock_and_key():
    loaded = load_content_world(YAML_WORLD)
    market = next(room for room in loaded.world.rooms if room.id == "market")

    door = market.exits["east"]

    assert isinstance(door, Exit)
    assert door.to == "tavern"
    assert door.locked is True
    assert door.key == "key_gold_1"


def test_seeder_carries_the_product_lock_on_both_sides():
    compiled = compile_world(load_content_world(YAML_WORLD))
    edges = {(edge.room_id, edge.direction): edge for edge in compiled.exits}

    east = edges[("market", "east")]
    assert east.target_id == "tavern"
    assert east.lock_id is not None
    assert compiled.locks[east.lock_id].key == "key_gold_1"

    west = edges[("tavern", "west")]
    assert west.target_id == "market"
    assert west.lock_id == east.lock_id
    assert compiled.locks[west.lock_id].key == "key_gold_1"


# --- validator: the content rule -------------------------------------------


def _check(tmp_path: Path, data: dict):
    path = tmp_path / "world.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return check_world(path)


def _two_rooms(exit_data: dict) -> dict:
    return {
        "name": "Locked",
        "start_room": "gate",
        "rng_seed": 7,
        "rooms": [
            {
                "id": "gate",
                "name": "Gate",
                "exits": {"north": exit_data},
            },
            {"id": "market", "name": "Market"},
        ],
    }


def test_validator_rejects_a_lock_without_a_key(tmp_path):
    errors = _check(tmp_path, _two_rooms({"to": "market", "locked": True}))

    assert any("needs a key" in error for error in errors)


def test_validator_accepts_a_one_way_lock(tmp_path):
    data = _two_rooms(
        {
            "to": "market",
            "locked": True,
            "key": "key_gold_1",
            "one_way": True,
        }
    )
    data["rooms"][0]["objects"] = [
        {"id": "key_gold_1", "name": "Gold Key", "description": "A key."}
    ]

    assert _check(tmp_path, data) == []
