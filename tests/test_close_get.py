"""Red-green: `close` and `get <item> from <container>` (SPEC v2.1.2 #76).

`open <container>` lifts the lid and lists what is inside, keeping the contents
in the container. `get <item> from <container>` pulls one item out into the
actor's inventory, and `close <container>` puts the lid back down. This is the
v2.1 container flow (#76); v2.0 spilled loot onto the room floor.

These tests are written before the verbs exist, so they fail now (RED).
"""

import json

import pytest

from src.mud.content import Entity, Room
from src.mud.content.models import GameObject
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.world.loader import load_world
from src.mud.world.seed import _object_state


def _market(*, container_open: bool = False, locked: bool = False):
    engine = Engine()
    room = Room("Market", "A busy market.")
    room.item_id = "market"
    chest = Entity("Chest", "A sturdy oak chest.", item_id="chest")
    # Set by the loader from the YAML in the product path; direct here so the
    # red run exercises the missing verbs rather than a constructor TypeError.
    chest.container = True
    chest.open = container_open
    chest.locked = locked
    coin = Entity("Coin", "A gold coin.", item_id="coin")
    chest.add_content(coin)
    room.add_content(chest)
    engine._rooms[room.name] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    return engine, player, room, chest, coin


def _events(result, type_=None, target=None):
    return [
        event for event in result.events
        if (type_ is None or event.type == type_)
        and (target is None or event.target == target)
    ]


# --- engine: close ---------------------------------------------------------


@pytest.mark.asyncio
async def test_open_then_close_leaves_the_container_closed():
    engine, _player, _room, chest, _coin = _market()

    opened = await engine.process_command("open chest", "Ada")
    assert chest.open is True
    assert "open" in opened.lower()

    closed = await engine.process_command("close chest", "Ada")

    assert closed == "You close the Chest."
    assert chest.open is False


@pytest.mark.asyncio
async def test_close_already_closed_refuses():
    engine, _player, _room, chest, _coin = _market()

    response = await engine.process_command("close chest", "Ada")

    assert "already closed" in response.lower()
    assert chest.open is False


@pytest.mark.asyncio
async def test_close_missing_container_refuses():
    engine, _player, room, chest, _coin = _market()
    room.remove_content(chest)

    response = await engine.process_command("close chest", "Ada")

    assert "no chest" in response.lower()
    assert chest.open is False


@pytest.mark.asyncio
async def test_close_a_plain_item_refuses():
    engine, _player, room, chest, coin = _market()
    chest.remove_content(coin)
    room.add_content(coin)

    response = await engine.process_command("close coin", "Ada")

    assert "not something you can close" in response.lower()


# --- engine: get from ------------------------------------------------------


@pytest.mark.asyncio
async def test_open_get_close_flow_keeps_contents_until_get():
    """The normal command path: open lists, get pulls one out, close shuts."""
    engine, player, room, chest, coin = _market(container_open=False)

    opened = await engine.process_command("open chest", "Ada")
    assert chest.open is True
    assert "coin" in opened.lower()
    assert coin in chest.contents
    assert coin not in room.contents

    got = await engine.process_command("get coin from chest", "Ada")
    assert got == "You get the Coin from the Chest."
    assert coin in player.contents
    assert coin not in chest.contents

    closed = await engine.process_command("close chest", "Ada")
    assert closed == "You close the Chest."
    assert chest.open is False


@pytest.mark.asyncio
async def test_get_from_a_closed_container_refuses():
    engine, player, _room, chest, coin = _market(container_open=False)

    response = await engine.process_command("get coin from chest", "Ada")

    assert "closed" in response.lower()
    assert coin in chest.contents
    assert coin not in player.contents


@pytest.mark.asyncio
async def test_get_missing_item_refuses():
    engine, player, _room, chest, coin = _market(container_open=True)
    chest.remove_content(coin)

    response = await engine.process_command("get coin from chest", "Ada")

    assert "not in the chest" in response.lower()
    assert coin not in player.contents


@pytest.mark.asyncio
async def test_get_fixed_item_refuses():
    engine, player, _room, chest, coin = _market(container_open=True)
    coin.portable = False

    response = await engine.process_command("get coin from chest", "Ada")

    assert "fixed in place" in response.lower()
    assert coin in chest.contents
    assert coin not in player.contents


@pytest.mark.asyncio
async def test_get_from_missing_container_refuses():
    engine, player, room, chest, coin = _market(container_open=True)
    room.remove_content(chest)

    response = await engine.process_command("get coin from chest", "Ada")

    assert "no chest" in response.lower()
    assert coin in chest.contents
    assert coin not in player.contents


@pytest.mark.asyncio
async def test_plain_get_still_takes_from_the_room():
    """Bare `get <item>` stays the v2.0 alias for `take`."""
    engine, player, room, chest, coin = _market()
    chest.remove_content(coin)
    room.add_content(coin)

    response = await engine.process_command("get coin", "Ada")

    assert "picked up" in response.lower()
    assert coin in player.contents


# --- protocol adapter: typed events and errors -----------------------------


@pytest.mark.asyncio
async def test_close_emits_object_closed_for_actor_and_room():
    engine, _player, _room, chest, _coin = _market()
    await execute(engine, "Ada", "open chest")

    result = await execute(engine, "Ada", "close chest")

    actor = _events(result, "object.closed", "actor")
    room = _events(result, "object.closed", "room")
    assert actor and room
    assert actor[0].data["object"]["id"] == "chest"
    assert actor[0].data["self"] is True
    assert room[0].data["self"] is False
    assert chest.open is False


@pytest.mark.asyncio
async def test_close_already_closed_is_typed():
    engine, _player, _room, _chest, _coin = _market()

    result = await execute(engine, "Ada", "close chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "already_closed"


@pytest.mark.asyncio
async def test_get_emits_object_got_and_inventory_follow_up():
    engine, player, _room, chest, coin = _market()
    await execute(engine, "Ada", "open chest")

    result = await execute(engine, "Ada", "get coin from chest")

    actor = _events(result, "object.got", "actor")
    room = _events(result, "object.got", "room")
    inventory = _events(result, "player.inventory", "actor")
    assert actor and room and inventory
    assert actor[0].data["object"]["id"] == "coin"
    assert actor[0].data["container"]["id"] == "chest"
    assert actor[0].data["self"] is True
    assert room[0].data["self"] is False
    assert inventory[0].data["items"] == [
        {"id": "coin", "name": "Coin", "weight": 0.0}]
    assert coin in player.contents
    assert coin not in chest.contents


@pytest.mark.asyncio
async def test_get_from_closed_is_typed():
    engine, _player, _room, _chest, _coin = _market(container_open=False)

    result = await execute(engine, "Ada", "get coin from chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "container_closed"


@pytest.mark.asyncio
async def test_get_missing_item_is_typed_not_here():
    engine, _player, _room, chest, coin = _market(container_open=True)
    chest.remove_content(coin)

    result = await execute(engine, "Ada", "get coin from chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "not_here"


@pytest.mark.asyncio
async def test_get_fixed_item_is_typed_not_portable():
    engine, _player, _room, _chest, coin = _market(container_open=True)
    coin.portable = False

    result = await execute(engine, "Ada", "get coin from chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "not_portable"


@pytest.mark.asyncio
async def test_get_missing_container_is_typed_not_found():
    engine, _player, room, chest, _coin = _market(container_open=True)
    room.remove_content(chest)

    result = await execute(engine, "Ada", "get coin from chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "not_found"


@pytest.mark.asyncio
async def test_open_twice_is_typed_already_open():
    engine, _player, _room, _chest, _coin = _market()
    await execute(engine, "Ada", "open chest")

    result = await execute(engine, "Ada", "open chest")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "already_open"


@pytest.mark.asyncio
async def test_room_state_exposes_the_container_open_flag():
    engine, _player, _room, chest, _coin = _market(container_open=True)

    result = await execute(engine, "Ada", "look")

    objects = result.events[0].data["room"]["objects"]
    view = next(obj for obj in objects if obj["id"] == "chest")
    assert view["container"] is True
    assert view["open"] is True
    assert chest.open is True


# --- authored container state ---------------------------------------------


@pytest.mark.asyncio
async def test_loader_carries_the_authored_open_state(tmp_path):
    """`state.open: true` reaches the engine, so `get from` works on load."""
    path = tmp_path / "world.yaml"
    path.write_text(
        """
name: Open World
start_room: cave
rng_seed: 7
rooms:
  - id: cave
    name: Cave
    description: A dry cave.
    objects:
      - id: chest
        name: chest
        description: An open chest.
        kind: container
        state:
          open: true
        contains: [coin]
      - id: coin
        name: coin
        description: A gold coin.
""",
        encoding="utf-8",
    )
    engine = Engine()
    await load_world(engine, path)
    chest = next(
        obj for obj in engine.rooms["Cave"].contents if obj.item_id == "chest")
    coin = next(
        obj for obj in chest.contents if obj.item_id == "coin")

    assert chest.open is True
    assert coin in chest.contents


def test_seeded_object_state_records_the_open_flag():
    """The seed row's JSON state carries `open`, alongside the other flags."""
    chest = GameObject(
        id="chest", name="Chest", kind="container",
        state={"open": True}, contains=["coin"],
    )

    state = json.loads(_object_state(chest))

    assert state["open"] is True
