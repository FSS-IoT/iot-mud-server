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
    assert coin not in room.contents


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
async def test_open_empty_container_emits_opened(vault):
    engine, _player, _room, chest, coin, _key = vault
    chest.remove_content(coin)
    chest.locked = False

    result = await execute(engine, "Ada", "open chest")

    assert _events(result, "object.opened", "actor")
    assert _events(result, "object.opened", "room")
    assert "empty" in result.events[0].data["text"].lower()


@pytest.mark.asyncio
async def test_use_already_open_container(vault):
    engine, _player, _room, chest, _coin, _key = vault
    chest.locked = False

    result = await execute(engine, "Ada", "use gold key on chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "already_open"


@pytest.mark.asyncio
async def test_read_in_dark_room_does_not_leak_content(vault):
    engine, _player, room, _chest, _coin, _key = vault
    cave = Room("Cave", "A pitch-black cave.", dark=True)
    cave.item_id = "cave"
    sign = Entity("Sign", "The secret word is BANANA.", readable=True,
                  item_id="sign")
    cave.add_content(sign)
    room.exits["down"] = cave
    engine._rooms["Cave"] = cave

    await execute(engine, "Ada", "go down")
    result = await execute(engine, "Ada", "read sign")

    assert [event.type for event in result.events] == ["notice"]
    assert "dark" in result.events[0].data["text"].lower()
    assert "BANANA" not in result.events[0].data["text"]


@pytest.mark.asyncio
async def test_examine_without_target_is_bad_args(vault):
    engine, _player, _room, _chest, _coin, _key = vault

    for command in ("examine", "look at", "look in"):
        result = await execute(engine, "Ada", command)
        assert result.events[0].type == "error"
        assert result.events[0].data["code"] == "bad_args"


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
async def test_duplicate_instance_ids_stay_stable(vault):
    """A coin keeps its instance id when it moves from the room to an inventory."""
    engine, player, room, chest, coin, _key = vault
    chest.remove_content(coin)
    room.add_content(coin)
    second = Entity("Gold Coin", "Another coin.", item_id="coin")
    room.add_content(second)

    first = await execute(engine, "Ada", "look")
    first_coins = [obj for obj in first.events[0].data["room"]["objects"]
                   if obj["kind"] == "coin"]
    assert [obj["id"] for obj in first_coins] == ["coin-1", "coin-2"]

    room.remove_content(coin)
    player.add_content(coin)

    inventory = await execute(engine, "Ada", "inventory")
    carried = [item for item in inventory.events[0].data["items"]
               if item["id"] == "coin-1"]
    assert carried

    second_state = await execute(engine, "Ada", "look")
    remaining = [obj for obj in second_state.events[0].data["room"]["objects"]
                 if obj["kind"] == "coin"]
    assert [obj["id"] for obj in remaining] == ["coin-2"]


@pytest.mark.asyncio
async def test_room_state_sorts_objects_and_players(vault):
    engine, _player, room, _chest, _coin, _key = vault
    room.add_content(Entity("Zebra", "A zebra."))
    room.add_content(Entity("Apple", "An apple."))
    engine.register_player("Zed")
    engine.register_player("Amy")

    result = await execute(engine, "Ada", "look")
    room_data = result.events[0].data["room"]
    object_names = [obj["name"] for obj in room_data["objects"]]
    player_names = [person["name"] for person in room_data["players"]]

    assert object_names == sorted(object_names, key=str.lower)
    assert player_names == ["Amy", "Zed"]


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
async def test_light_ignites_a_carried_source_and_lights_the_room(vault):
    engine, player, room, _chest, _coin, _key = vault
    cave = Room("Cave", "A pitch-black cave.", dark=True)
    cave.item_id = "cave"
    cave.add_content(Entity("Gold Coin", "A coin.", item_id="cave_coin"))
    room.exits["down"] = cave
    engine._rooms["Cave"] = cave
    torch = Entity("Torch", "A torch.", item_id="torch", light_source=True)
    player.add_content(torch)

    await execute(engine, "Ada", "go down")
    dark = await execute(engine, "Ada", "look")
    assert dark.events[0].data["room"]["dark"] is True

    result = await execute(engine, "Ada", "light torch")
    actor = _events(result, "object.lit", "actor")
    broadcast = _events(result, "object.lit", "room")
    assert actor and broadcast
    assert actor[0].data["object"]["lit"] is True
    assert actor[0].data["self"] is True
    assert broadcast[0].data["self"] is False
    assert torch.lit is True

    lit = await execute(engine, "Ada", "look")
    assert lit.events[0].data["room"]["dark"] is False
    assert any(obj["name"] == "Gold Coin"
               for obj in lit.events[0].data["room"]["objects"])


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
