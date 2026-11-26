"""Red-green: `look in <chest>` / `look inside <chest>`.

`look` shows the room; `look in <container>` and `look inside <container>`
instead reveal what a container is holding. Locked containers refuse. These
behave do not exist yet, so they fail now (RED).
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


async def _setup():
    engine = Engine()
    room = Room("Market", "A busy marketplace.")
    engine.start_room = room
    _player = engine.register_player("Ada")
    return engine, _player, room


@pytest.mark.asyncio
async def test_look_in_reveals_contents():
    """`look in <chest>` reveals a container's contents."""
    engine, _player, room = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    chest = Entity("Chest", "A sturdy oak chest.")
    chest.add_content(lantern)
    room.add_content(chest)

    response = await engine.process_command("look in chest", "Ada")

    assert "lantern" in response.lower()


@pytest.mark.asyncio
async def test_look_inside_reveals_contents():
    """`look inside <chest>` is a synonym for `look in <chest>`."""
    engine, _player, room = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    chest = Entity("Chest", "A sturdy oak chest.")
    chest.add_content(lantern)
    room.add_content(chest)

    response = await engine.process_command("look inside chest", "Ada")

    assert "lantern" in response.lower()


@pytest.mark.asyncio
async def test_look_in_locked_refused():
    """A locked container refuses to be looked inside."""
    engine, _player, room = await _setup()
    chest = Entity("Chest", "A sturdy oak chest.", locked=True, required_key="key_gold_1")
    room.add_content(chest)

    response = await engine.process_command("look in chest", "Ada")

    assert "lock" in response.lower()


@pytest.mark.asyncio
async def test_look_in_empty():
    """Looking inside an empty container reports it is empty."""
    engine, _player, room = await _setup()
    chest = Entity("Chest", "A sturdy oak chest.")
    room.add_content(chest)

    response = await engine.process_command("look in chest", "Ada")

    assert "empty" in response.lower()
