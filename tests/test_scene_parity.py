"""Parity between the prose renderer and the v2 wire adapter (#135).

One `Scene` value carries what an actor can see: room identity, visible
objects, other players, NPCs, exits (with locks and labels), geometry,
darkness, and the actor's inventory. `look`, movement/inventory prose, and
`room.state` all render from that one value, so the sets cannot disagree.

The old code let the adapter re-walk the room. These tests pin the parity in
lit, dark, and geometry rooms, and prove darkness is applied once.
"""

import pytest

from src.mud.content import NPC, Entity, Room
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import build_room_state

GEOMETRY = {
    "cells": ["###+###", "#.....#", "#######"],
    "spawn": {"north": [3, 1]},
}


def _world(*, dark: bool = False, geometry: dict | None = None,
           lit_torch: bool = False):
    """Ada and Ben in a hall with a coin, an NPC, and a north exit."""
    engine = Engine()
    hall = Room("Hall", "A long stone hall.", dark=dark)
    hall.item_id = "hall"
    hall.geometry = geometry
    vault = Room("Vault", "A cold vault.")
    vault.item_id = "vault"
    hall.exits["north"] = vault
    engine._rooms["Hall"] = hall
    engine._rooms["Vault"] = vault
    engine.start_room = hall

    coin = Entity("Gold Coin", "A minted coin.", item_id="coin", weight=0.01)
    hall.add_content(coin)
    npc = NPC("Mira the Merchant", "A merchant.", item_id="npc_merchant")
    hall.npcs.append(npc)

    ada = engine.register_player("Ada")
    engine.register_player("Ben")
    key = Entity("Brass Key", "A small key.", item_id="brass_key")
    ada.add_content(key)
    if lit_torch:
        torch = Entity("Torch", "A wooden torch.", item_id="torch",
                       light_source=True, lit=True,
                       equip_slots=("hand",), hands=1)
        ada.add_content(torch)
        ada.equip(torch, "hand_1")
    return engine, ada, hall, vault, coin, npc, key


def _state_lists(state):
    room = state["room"]
    return {
        "objects": sorted(obj["name"] for obj in room["objects"]),
        "players": sorted(person["name"] for person in room["players"]),
        "npcs": sorted(npc["name"] for npc in room["npcs"]),
        "exits": sorted(
            (entry["direction"], entry["to"], entry["locked"],
             entry.get("label"))
            for entry in room["exits"]
        ),
    }


def _scene_lists(scene):
    return {
        "objects": sorted(obj.name for obj in scene.objects),
        "players": sorted(person.name for person in scene.occupants),
        "npcs": sorted(npc.name for npc in scene.npcs),
        "exits": sorted(
            (exit_.direction, exit_.to_id, exit_.locked, exit_.label)
            for exit_ in scene.exits
        ),
    }


@pytest.mark.parametrize(
    "dark,geometry",
    [
        (False, None),
        (True, None),
        (False, GEOMETRY),
    ],
    ids=["lit", "dark", "geometry"],
)
@pytest.mark.asyncio
async def test_scene_agrees_between_look_and_room_state(dark, geometry):
    """`look` prose and `room.state` expose the same visible sets."""
    engine, ada, _hall, _vault, _coin, _npc, _key = _world(
        dark=dark, geometry=geometry)

    scene = engine.scene_for(ada)
    state = build_room_state(engine, ada)
    look = await engine.execute(ada, "look")

    assert scene.dark is dark
    assert _state_lists(state) == _scene_lists(scene)

    if dark:
        # No light: both renderers report darkness and hide the room.
        assert state["room"]["dark"] is True
        assert state["room"]["objects"] == []
        assert state["room"]["players"] == []
        assert state["room"]["npcs"] == []
        assert state["room"]["exits"] == []
        assert "geometry" not in state["room"]
        assert "dark" in look.reply.lower()
    else:
        assert state["room"]["dark"] is False
        assert "Gold Coin" in look.reply
        assert "Mira the Merchant" in look.reply
        assert "Ben" in look.reply
        if geometry is None:
            assert "geometry" not in state["room"]
        else:
            assert scene.geometry == geometry
            assert state["room"]["geometry"] == geometry


@pytest.mark.asyncio
async def test_darkness_is_applied_once_with_a_lit_torch():
    """A carried lit torch lights the same set for both renderers."""
    engine, ada, room, _vault, _coin, _npc, _key = _world(
        dark=True, lit_torch=True)

    scene = engine.scene_for(ada)
    state = build_room_state(engine, ada)
    look = await engine.execute(ada, "look")

    assert room.dark is True
    assert scene.dark is False
    assert state["room"]["dark"] is False
    assert _state_lists(state) == _scene_lists(scene)
    assert "Gold Coin" in {obj["name"] for obj in state["room"]["objects"]}
    assert "Mira the Merchant" in {
        npc["name"] for npc in state["room"]["npcs"]}
    assert "Ben" in {person["name"] for person in state["room"]["players"]}
    assert "Gold Coin" in look.reply
    assert "dark" not in look.reply.lower()


@pytest.mark.asyncio
async def test_inventory_prose_and_wire_items_match_the_scene():
    """The carried set is one Scene inventory, rendered two ways."""
    engine, ada, _room, _vault, _coin, _npc, _key = _world()

    scene = engine.scene_for(ada)
    reply = await engine.execute(ada, "inventory")
    result = await _inventory_event(engine, ada)

    assert sorted(item.name for item in scene.inventory) == ["Brass Key"]
    assert [item["name"] for item in result] == ["Brass Key"]
    assert "Brass Key" in reply.reply


@pytest.mark.asyncio
async def test_go_into_a_dark_room_hides_the_destination():
    """Moving into an unlit room hides the destination in prose and state.

    #135 is widened to cover this intentional v1 prose change: v1 retires in
    #137, and v2 already sends a dark `room.state` with no objects or exits.
    """
    engine, ada, hall, vault, _coin, _npc, _key = _world()
    vault.dark = True
    vault.exits["south"] = hall
    vault.add_content(
        Entity("Vault Coin", "A coin in the dark.", item_id="vault_coin"))

    outcome = await engine.execute(ada, "go north")

    assert "It is too dark to see anything." in outcome.reply
    assert "A cold vault." not in outcome.reply
    assert "Exits lead" not in outcome.reply
    assert "Vault Coin" not in outcome.reply

    state = build_room_state(engine, ada)
    room = state["room"]
    assert room["dark"] is True
    assert room["objects"] == []
    assert room["players"] == []
    assert room["npcs"] == []
    assert room["exits"] == []
    assert "geometry" not in room


async def _inventory_event(engine, ada):
    """Run `inventory` through the v2 adapter and return its items."""
    from src.mud.protocol.adapter import execute

    result = await execute(engine, ada.name, "inventory")
    inventory = [event for event in result.events
                 if event.type == "player.inventory"]
    assert len(inventory) == 1
    return inventory[0].data["items"]



