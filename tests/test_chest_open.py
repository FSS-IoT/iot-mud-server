"""Red-green: `open <chest>` — open a container and list its contents.

`use <key> on <lock>` unlocks; `open <chest>` then reveals what is inside.
A container holds its contents; opening lists them and keeps them inside.
`get <item> from <chest>` pulls one out. v2.1 does not spill loot into the
room (see #76); `take` stays the room-floor verb.

These tests expect an `open` verb that does not exist yet, so they fail now (RED).
"""

import pytest

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine


async def _setup():
    engine = Engine()
    player = Player("Ada", "A brave adventurer.")
    room = Room("Market", "A busy marketplace.")
    engine.player = player
    engine._player_location = room
    events = []
    await engine.subscribe("chest_opened", lambda *_: events.append(True))
    return engine, player, room, events


@pytest.mark.asyncio
async def test_open_locked_chest_refused():
    """An unlocked-by-key-less chest refuses to open."""
    engine, _player, room, _events = await _setup()
    chest = Entity("Chest", "A sturdy oak chest.", locked=True, required_key="key_gold_1")
    room.add_content(chest)

    response = await engine.process_command("open chest")

    assert "lock" in response.lower()
    assert chest.locked is True


@pytest.mark.asyncio
async def test_open_unlocked_chest_reveals_contents():
    """Opening an unlocked chest lists its contents and keeps them inside."""
    engine, _player, room, events = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    chest = Entity("Chest", "A sturdy oak chest.")
    chest.add_content(lantern)
    room.add_content(chest)

    response = await engine.process_command("open chest")

    assert "lantern" in response.lower()
    assert lantern in chest.contents  # stays inside; `get` pulls it out
    assert lantern not in room.contents
    assert events == [True]


@pytest.mark.asyncio
async def test_get_after_open():
    """Loot revealed by opening can then be pulled out with `get ... from`."""
    engine, player, room, _events = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    chest = Entity("Chest", "A sturdy oak chest.")
    chest.add_content(lantern)
    room.add_content(chest)

    await engine.process_command("open chest")
    response = await engine.process_command("get lantern from chest")

    assert lantern in player.contents
    assert lantern not in chest.contents
    assert "get" in response.lower()


@pytest.mark.asyncio
async def test_open_empty_chest():
    """Opening a container with nothing inside reports it is empty."""
    engine, _player, room, _events = await _setup()
    chest = Entity("Chest", "A sturdy oak chest.")
    room.add_content(chest)

    response = await engine.process_command("open chest")

    assert "empty" in response.lower()
