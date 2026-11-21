"""Unit tests for the protocol-v2 command adapter.

The adapter turns one engine command into typed v2 events without changing the
engine's authoritative reply. These tests use a small world (a locked chest, a
coin, a key) so the event contract is exercised directly, without sockets.
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute


@pytest.fixture
def vault():
    engine = Engine()
    room = Room("Vault", "A stone vault.")
    room.item_id = "vault"

    chest = Entity("Chest", "An oak chest.", item_id="chest",
                   locked=True, required_key="gold_key")
    coin = Entity("Coin", "A gold coin.", item_id="coin")
    chest.add_content(coin)
    key = Entity("Gold Key", "A small key.", item_id="gold_key")
    room.add_content(chest)
    engine._rooms[room.name] = room
    engine._player_location = room
    player = engine.register_player("Ada")
    player.add_content(key)
    return engine, player, room, chest, coin, key


def _events(result, type_=None, target=None):
    return [event for event in result.events
            if (type_ is None or event.type == type_)
            and (target is None or event.target == target)]


@pytest.mark.asyncio
async def test_look_emits_structured_room_state(vault):
    engine, _player, _room, _chest, _coin, _key = vault

    result = await execute(engine, "Ada", "look")

    assert [event.type for event in result.events] == ["room.state"]
    state = result.events[0].data["room"]
    assert state["id"] == "vault"
    assert state["name"] == "Vault"
    assert {"id": "chest", "kind": "chest", "name": "Chest"} in [
        {"id": obj["id"], "kind": obj["kind"], "name": obj["name"]}
        for obj in state["objects"]
    ]
    assert state["dark"] is False


@pytest.mark.asyncio
async def test_take_emits_object_event_and_inventory(vault):
    engine, player, room, chest, coin, _key = vault
    chest.remove_content(coin)
    room.add_content(coin)

    result = await execute(engine, "Ada", "take coin")

    actor = _events(result, "object.taken", "actor")
    broadcast = _events(result, "object.taken", "room")
    inventory = _events(result, "player.inventory", "actor")

    assert len(actor) == 1 and len(broadcast) == 1 and len(inventory) == 1
    assert actor[0].data["object"]["id"] == "coin"
    assert actor[0].data["self"] is True
    assert broadcast[0].data["self"] is False
    assert "Ada picks up" in broadcast[0].data["text"]
    assert coin in player.contents


@pytest.mark.asyncio
async def test_use_unlocks_container_then_open_spills_loot(vault):
    engine, _player, room, chest, coin, _key = vault

    unlocked = await execute(engine, "Ada", "use gold key on chest")
    assert _events(unlocked, "object.unlocked", "actor")
    assert _events(unlocked, "object.unlocked", "room")
    assert chest.locked is False

    opened = await execute(engine, "Ada", "open chest")
    assert _events(opened, "object.opened", "actor")
    assert _events(opened, "object.opened", "room")
    assert coin in room.contents


@pytest.mark.asyncio
async def test_put_emits_object_event_and_inventory(vault):
    engine, _player, room, chest, coin, _key = vault
    chest.remove_content(coin)
    room.add_content(coin)
    chest.locked = False
    await execute(engine, "Ada", "take coin")

    result = await execute(engine, "Ada", "put coin in chest")

    actor = _events(result, "object.put", "actor")
    broadcast = _events(result, "object.put", "room")
    inventory = _events(result, "player.inventory", "actor")

    assert actor and broadcast and inventory
    assert actor[0].data["container"]["id"] == "chest"
    assert actor[0].data["self"] is True
    assert broadcast[0].data["self"] is False
    assert coin in chest.contents


@pytest.mark.asyncio
async def test_say_emits_actor_and_room_copies(vault):
    engine, _player, _room, _chest, _coin, _key = vault

    result = await execute(engine, "Ada", "say hello")

    actor = _events(result, "player.said", "actor")
    broadcast = _events(result, "player.said", "room")

    assert actor[0].data["self"] is True
    assert broadcast[0].data["self"] is False
    assert actor[0].data["text"] == 'You say, "hello".'
    assert broadcast[0].data["text"] == 'Ada says, "hello".'


@pytest.mark.asyncio
async def test_unknown_command_is_an_error(vault):
    engine, _player, _room, _chest, _coin, _key = vault

    result = await execute(engine, "Ada", "dance")

    assert [event.type for event in result.events] == ["error"]
    assert result.events[0].data["code"] == "unknown_command"


@pytest.mark.asyncio
async def test_locked_container_refuses_until_unlocked(vault):
    engine, _player, _room, _chest, _coin, _key = vault

    refused = await execute(engine, "Ada", "open chest")
    assert refused.events[0].type == "error"
    assert refused.events[0].data["code"] == "locked"

    await execute(engine, "Ada", "use gold key on chest")
    looked = await execute(engine, "Ada", "look in chest")
    assert looked.events[0].type == "notice"
    assert "Coin" in looked.events[0].data["text"]


@pytest.mark.asyncio
async def test_exits_uses_the_registered_player_room(vault):
    engine, _player, room, _chest, _coin, _key = vault
    hall = Room("Hall", "A bare hall.")
    hall.item_id = "hall"
    room.exits["north"] = hall
    engine._rooms["Hall"] = hall

    moved = await execute(engine, "Ada", "go north")
    assert moved.events[0].type == "room.state"
    assert moved.events[0].data["room"]["id"] == "hall"

    result = await execute(engine, "Ada", "exits")
    assert result.events[0].type == "notice"
    assert "no exits" in result.events[0].data["text"].lower()
    assert "Marketplace" not in result.events[0].data["text"]


@pytest.mark.asyncio
async def test_dark_room_hides_objects(vault):
    engine, _player, room, _chest, _coin, _key = vault
    cave = Room("Cave", "A pitch-black cave.", dark=True)
    cave.item_id = "cave"
    room.exits["down"] = cave
    engine._rooms["Cave"] = cave

    await execute(engine, "Ada", "go down")
    result = await execute(engine, "Ada", "look")

    state = result.events[0].data["room"]
    assert state["dark"] is True
    assert state["objects"] == []
    assert state["players"] == []
    assert "dark" in result.events[0].data["text"].lower()
