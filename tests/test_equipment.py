"""Ticket #181: full body equipment slots and equipped light.

A light source lights a dark room only when it is equipped in a hand, on the
belt, or on the head. A torch in the pack does nothing. Hands are a real
resource: a torch takes one, a two-handed item takes both, and a lantern can
hang on the belt.

Red on the parent: `Entity` has no `equip_slots`/`hands`, `Player` has no
equipment, and no `equip`/`unequip` commands exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.mud.content import Entity, Room
from src.mud.content.models import GameObject
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.storage.db import open_database
from src.mud.storage.store import WorldStore
from src.mud.world.loader import load_world
from src.mud.world.seed import seed_world
from tests.golden.harness import HarnessClient, server_harness

WORLD = """
name: Equipment Test
start_room: hall
rng_seed: 7
rooms:
  - id: hall
    name: Hall
    description: A stone hall.
    exits:
      north: cave
    objects:
      - id: torch
        name: Torch
        description: A wooden torch.
        weight: 0.5
        light_source: true
        equip_slots: [hand]
        hands: 1
      - id: lantern
        name: Lantern
        description: A brass lantern.
        weight: 0.4
        light_source: true
        equip_slots: [hand, belt]
        hands: 1
      - id: sword
        name: Sword
        description: A two-handed sword.
        weight: 3.0
        equip_slots: [hand]
        hands: 2
  - id: cave
    name: Cave
    description: A dark cave.
    dark: true
"""


def _world_file(tmp_path: Path) -> Path:
    path = tmp_path / "world.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


def _engine(dark: bool = True):
    engine = Engine()
    room = Room("Cave", "A pitch-black cavern.", dark=dark)
    room.item_id = "cave"
    room.add_content(Entity("Coin", "A small coin.", item_id="coin"))
    engine._rooms["Cave"] = room
    engine.start_room = room
    ada = engine.register_player("Ada")
    torch = Entity(
        "Torch", "A wooden torch.", light_source=True,
        equip_slots=("hand",), hands=1, item_id="torch")
    lantern = Entity(
        "Lantern", "A brass lantern.", light_source=True,
        equip_slots=("hand", "belt"), hands=1, item_id="lantern")
    sword = Entity(
        "Sword", "A two-handed sword.",
        equip_slots=("hand",), hands=2, item_id="sword")
    stick = Entity(
        "Stick", "A short stick.",
        equip_slots=("hand",), hands=1, item_id="stick")
    for item in (torch, lantern, sword, stick):
        item.instance_id = item.item_id
        ada.add_content(item)
    return engine, ada, torch, lantern, sword, stick


async def _code(engine, command: str) -> str | None:
    result = await execute(engine, "Ada", command)
    error = next(
        (event for event in result.events if event.type == "error"), None)
    return error.data.get("code") if error is not None else None


async def test_a_torch_takes_one_hand_and_a_second_fills_the_other():
    engine, ada, torch, _lantern, _sword, stick = _engine()

    assert await engine.process_command("equip torch", "Ada") == (
        "You equip the Torch in your hand.")
    assert ada.equipment["hand_1"] is torch
    assert torch.equipped_slot == "hand_1"

    await engine.process_command("equip stick", "Ada")
    assert ada.equipment["hand_2"] is stick

    assert await _code(engine, "equip sword") == "no_slot"


async def test_a_two_handed_item_blocks_both_hands():
    engine, ada, torch, _lantern, sword, _stick = _engine()

    await engine.process_command("equip sword", "Ada")
    assert ada.equipment["hand_1"] is sword
    assert sword.equipped_slot == "hand_1"

    assert await _code(engine, "equip torch") == "no_slot"
    assert torch.equipped_slot is None


async def test_a_lantern_on_the_belt_leaves_both_hands_free():
    engine, ada, torch, lantern, _sword, stick = _engine()

    await engine.process_command("equip lantern on belt", "Ada")
    await engine.process_command("equip torch", "Ada")
    await engine.process_command("equip stick", "Ada")

    assert ada.equipment["belt"] is lantern
    assert ada.equipment["hand_1"] is torch
    assert ada.equipment["hand_2"] is stick


async def test_a_torch_cannot_hang_on_the_belt():
    engine, _ada, _torch, _lantern, _sword, _stick = _engine()

    assert await _code(engine, "equip torch on belt") == "bad_args"


async def test_a_lit_torch_in_the_pack_does_not_light_a_dark_room():
    engine, _ada, torch, _lantern, _sword, _stick = _engine()
    torch.lit = True

    assert "dark" in (await engine.process_command("look", "Ada")).lower()

    await engine.process_command("equip torch", "Ada")
    response = await engine.process_command("look", "Ada")

    assert "dark" not in response.lower()
    assert "Here you see" in response


async def test_light_auto_equips_a_torch():
    engine, ada, torch, _lantern, _sword, _stick = _engine()

    await engine.process_command("light torch", "Ada")

    assert torch.lit is True
    assert torch.equipped_slot == "hand_1"
    assert ada.equipment["hand_1"] is torch
    assert "dark" not in (await engine.process_command("look", "Ada")).lower()


async def test_light_uses_the_belt_when_both_hands_are_full():
    engine, ada, torch, lantern, _sword, _stick = _engine()
    other = Entity("Other", "Another stick.", equip_slots=("hand",), hands=1)
    ada.add_content(other)

    await engine.process_command("equip stick", "Ada")
    await engine.process_command("equip other", "Ada")
    assert "hand_1" in ada.equipment and "hand_2" in ada.equipment

    await engine.process_command("light lantern", "Ada")

    assert lantern.equipped_slot == "belt"
    assert lantern.lit is True
    assert torch.equipped_slot is None


async def test_light_with_no_free_light_slot_is_a_typed_error():
    engine, _ada, torch, _lantern, _sword, _stick = _engine()
    other = Entity("Other", "Another stick.", equip_slots=("hand",), hands=1)
    engine.get_player("Ada").add_content(other)

    await engine.process_command("equip stick", "Ada")
    await engine.process_command("equip other", "Ada")

    result = await execute(engine, "Ada", "light torch")

    assert [(event.type, event.data.get("code")) for event in result.events] == [
        ("error", "no_slot"),
    ]
    assert torch.lit is False
    assert torch.equipped_slot is None


async def test_inventory_marks_an_equipped_slot():
    engine, _ada, _torch, _lantern, _sword, _stick = _engine()
    await engine.process_command("equip torch", "Ada")

    result = await execute(engine, "Ada", "inventory")
    inventory = next(
        event for event in result.events if event.type == "player.inventory")
    torch_data = next(
        item for item in inventory.data["items"] if item["id"] == "torch")
    assert torch_data["slot"] == "hand_1"

    await engine.process_command("unequip torch", "Ada")
    result = await execute(engine, "Ada", "inventory")
    inventory = next(
        event for event in result.events if event.type == "player.inventory")
    torch_data = next(
        item for item in inventory.data["items"] if item["id"] == "torch")
    assert "slot" not in torch_data


async def test_dropping_an_equipped_item_clears_the_slot(tmp_path):
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    conn = open_database(tmp_path / "mud.db")
    try:
        from src.mud.storage.store import WriteThroughStore

        engine.attach_store(WriteThroughStore(conn))
        await engine.process_command("equip torch", "Ada")
        await engine.process_command("light torch", "Ada")
        await engine.process_command("drop torch", "Ada")

        assert torch.equipped_slot is None
        assert "hand_1" not in ada.equipment
        assert WorldStore(conn).object_location("torch") is not None
        state = conn.execute(
            "SELECT state FROM objects WHERE id = 'torch'"
        ).fetchone()
        assert json.loads(state["state"])["slot"] is None
    finally:
        conn.close()


async def test_equipped_state_survives_a_restart(tmp_path):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    conn = open_database(db_path)
    try:
        seed_world(conn, world_path, epoch="equipment")
    finally:
        conn.close()

    engine = Engine()
    await load_world(engine, world_path)
    async with server_harness(engine, db_path=db_path) as server, \
            HarnessClient(server) as ada:
        key = await ada.register_key("Ada")
        await ada.input("take torch", 2)
        await ada.next("object.taken", msg_id=2)
        await ada.input("equip torch", 3)
        await ada.next("player.inventory", msg_id=3)
        await ada.input("light torch", 4)
        await ada.next("object.lit")

    engine = Engine()
    await load_world(engine, world_path)
    async with server_harness(engine, db_path=db_path) as server, \
            HarnessClient(server) as ada:
        await ada.login(key, "login")
        inventory = await ada.inventory()
        torch = next(item for item in inventory if item["id"] == "torch")
        assert torch["slot"] == "hand_1"
        assert torch["lit"] is True


def test_content_model_accepts_and_validates_equipment_fields():
    item = GameObject(
        id="lantern", name="Lantern", equip_slots=["hand", "belt"], hands=1)
    assert item.equip_slots == ["hand", "belt"]
    assert item.hands == 1

    with pytest.raises(ValidationError):
        GameObject(id="bad", name="Bad", equip_slots=["tail"])
    with pytest.raises(ValidationError):
        GameObject(id="bad", name="Bad", equip_slots=["belt"], hands=2)


async def test_unequip_refuses_a_lit_source():
    engine, _ada, torch, _lantern, _sword, _stick = _engine()
    await engine.process_command("light torch", "Ada")

    assert await _code(engine, "unequip torch") == "still_lit"
    assert torch.equipped_slot == "hand_1"
    assert torch.lit is True


async def test_extinguish_lets_a_lit_source_be_stowed():
    engine, _ada, torch, _lantern, _sword, _stick = _engine()
    await engine.process_command("light torch", "Ada")

    assert await engine.process_command("extinguish torch", "Ada") == (
        "You put out the Torch.")
    assert torch.lit is False
    await engine.process_command("unequip torch", "Ada")
    assert torch.equipped_slot is None


async def test_put_refuses_a_lit_source():
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    chest = Entity(
        "Chest", "A chest.", container=True, item_id="chest")
    ada.location.add_content(chest)
    await engine.process_command("light torch", "Ada")

    assert await _code(engine, "put torch in chest") == "still_lit"
    assert chest.contents == []

    await engine.process_command("extinguish torch", "Ada")
    await engine.process_command("put torch in chest", "Ada")
    assert torch in chest.contents


async def test_a_dropped_lit_torch_stays_lit_and_lights_the_room():
    engine, _ada, torch, _lantern, _sword, _stick = _engine()
    await engine.process_command("light torch", "Ada")
    await engine.process_command("drop torch", "Ada")

    assert torch.lit is True
    assert torch.equipped_slot is None
    response = await engine.process_command("look", "Ada")
    assert "dark" not in response.lower()
    assert "Here you see" in response


async def test_taking_a_lit_torch_auto_equips_it():
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    room = ada.location
    ada.remove_content(torch)
    room.add_content(torch)
    torch.lit = True

    await engine.process_command("take torch", "Ada")

    assert torch in ada.contents
    assert torch not in room.contents
    assert torch.equipped_slot in ("hand_1", "hand_2")
    assert torch.lit is True


async def test_taking_a_lit_torch_with_no_light_slot_is_refused():
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    other = Entity("Other", "Another stick.", equip_slots=("hand",), hands=1)
    ada.add_content(other)
    await engine.process_command("equip stick", "Ada")
    await engine.process_command("equip other", "Ada")
    room = ada.location
    ada.remove_content(torch)
    room.add_content(torch)
    torch.lit = True

    assert await _code(engine, "take torch") == "no_slot"
    assert torch in room.contents
    assert torch not in ada.contents


async def test_two_lit_torches_can_be_taken_one_per_hand():
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    room = ada.location
    other = Entity(
        "Other Torch", "Another wooden torch.", light_source=True,
        equip_slots=("hand",), hands=1, item_id="other_torch")
    room.add_content(other)
    ada.remove_content(torch)
    room.add_content(torch)
    torch.lit = True
    other.lit = True

    await engine.process_command("take torch", "Ada")
    await engine.process_command("take other torch", "Ada")

    assert ada.equipment["hand_1"] is torch
    assert ada.equipment["hand_2"] is other
    assert torch.lit is True and other.lit is True


async def test_equip_on_hand_swaps_an_unlit_item_to_the_pack():
    engine, ada, torch, lantern, _sword, _stick = _engine()
    await engine.process_command("equip torch", "Ada")
    await engine.process_command("equip lantern", "Ada")
    assert ada.equipment["hand_1"] is torch
    assert ada.equipment["hand_2"] is lantern

    await engine.process_command("equip lantern on hand 1", "Ada")

    assert ada.equipment["hand_1"] is lantern
    assert torch.equipped_slot is None
    assert torch in ada.contents


async def test_swapping_out_a_lit_item_is_refused():
    engine, ada, torch, lantern, _sword, _stick = _engine()
    await engine.process_command("equip torch", "Ada")
    await engine.process_command("light torch", "Ada")
    await engine.process_command("equip lantern", "Ada")

    assert await _code(engine, "equip lantern on hand 1") == "still_lit"
    assert ada.equipment["hand_1"] is torch
    assert ada.equipment["hand_2"] is lantern


async def test_a_lit_source_cannot_move_to_a_non_light_slot():
    engine, ada, _torch, _lantern, _sword, _stick = _engine()
    lamp = Entity(
        "Lamp", "A lit lamp.", light_source=True, lit=True,
        equip_slots=("hand", "back"), hands=1, item_id="lamp")
    ada.add_content(lamp)
    await engine.process_command("equip lamp", "Ada")
    assert lamp.equipped_slot in ("hand_1", "hand_2")

    assert await _code(engine, "equip lamp on back") == "bad_args"
    assert lamp.equipped_slot in ("hand_1", "hand_2")


async def test_a_two_handed_item_swaps_out_one_unlit_hand_item():
    engine, ada, torch, _lantern, sword, _stick = _engine()
    await engine.process_command("equip torch", "Ada")

    await engine.process_command("equip sword on hand 1", "Ada")

    assert ada.equipment["hand_1"] is sword
    assert torch.equipped_slot is None
    assert torch in ada.contents


async def test_a_two_handed_item_swaps_out_both_unlit_hand_items():
    engine, ada, torch, lantern, sword, _stick = _engine()
    await engine.process_command("equip torch", "Ada")
    await engine.process_command("equip lantern", "Ada")

    await engine.process_command("equip sword on hand 1", "Ada")

    assert ada.equipment["hand_1"] is sword
    assert torch.equipped_slot is None
    assert lantern.equipped_slot is None


async def test_a_two_handed_swap_refuses_a_lit_hand_item():
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    await engine.process_command("equip torch", "Ada")
    await engine.process_command("light torch", "Ada")

    assert await _code(engine, "equip sword on hand 1") == "still_lit"
    assert ada.equipment["hand_1"] is torch


async def test_a_swap_clears_the_stowed_slot_in_the_store(tmp_path):
    from src.mud.storage.store import WriteThroughStore

    engine, _ada, _torch, _lantern, _sword, _stick = _engine()
    conn = open_database(tmp_path / "swap.db")
    try:
        engine.attach_store(WriteThroughStore(conn))
        await engine.process_command("equip torch", "Ada")
        await engine.process_command("equip lantern", "Ada")
        await engine.process_command("equip lantern on hand 1", "Ada")

        store = WorldStore(conn)
        assert store.object_location("torch") is not None
        torch_state = json.loads(conn.execute(
            "SELECT state FROM objects WHERE id = 'torch'").fetchone()["state"])
        lantern_state = json.loads(conn.execute(
            "SELECT state FROM objects WHERE id = 'lantern'").fetchone()["state"])
        assert torch_state["slot"] is None
        assert lantern_state["slot"] == "hand_1"
    finally:
        conn.close()


@pytest.mark.parametrize("equipment", [
    {"equip_slots": ["tail"], "hands": 1},
    {"equip_slots": ["hand"], "hands": 3},
    {"equip_slots": ["belt"], "hands": 2},
])
async def test_json_loader_rejects_bad_equipment(tmp_path, equipment):
    from src.mud.world.geometry import WorldDataError

    data = {
        "name": "Bad Equipment",
        "start_room": "hall",
        "rng_seed": 1,
        "items": [{"id": "bad", "name": "Bad", "description": "", **equipment}],
        "rooms": [{"name": "Hall", "description": "", "exits": {}}],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(WorldDataError):
        await load_world(Engine(), path)


async def test_extinguish_rejects_a_non_light_and_reports_an_unlit_source():
    engine, ada, torch, _lantern, _sword, _stick = _engine()
    coin = Entity("Coin", "A coin.", item_id="coin")
    ada.add_content(coin)

    assert await _code(engine, "extinguish coin") == "bad_args"
    response = await engine.process_command("extinguish torch", "Ada")

    assert "already out" in response.lower()
    assert torch.lit is False
