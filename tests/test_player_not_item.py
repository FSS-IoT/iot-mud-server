"""Players are not items.

A registered Player lives in Room.contents so movement and presence work, but
the item helpers (take/drop/put/use/open/look in) must never resolve a player
as a target. This is the engine-level regression for the v2 player-target bug.
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


def _market():
    engine = Engine()
    room = Room("Market", "A busy market.")
    coin = Entity("Gold Coin", "A coin.", item_id="coin")
    key = Entity("Gold Key", "A key.", item_id="gold_key")
    chest = Entity("Chest", "A chest.", item_id="chest",
                   locked=True, required_key="gold_key")
    room.add_content(coin)
    room.add_content(chest)
    engine._rooms[room.name] = room
    engine._player_location = room
    ada = engine.register_player("Ada")
    ben = engine.register_player("Ben")
    ben.add_content(key)
    return engine, room, coin, chest, ada, ben


@pytest.mark.asyncio
async def test_take_player_is_refused():
    engine, room, _coin, _chest, ada, ben = _market()

    response = await engine.process_command("take Ada", "Ben")

    assert "no ada" in response.lower()
    assert ada in room.contents
    assert ada.location is room
    assert ada not in ben.contents


@pytest.mark.asyncio
async def test_open_player_is_refused():
    engine, room, _coin, _chest, ada, _ben = _market()

    response = await engine.process_command("open Ada", "Ben")

    assert "no ada" in response.lower()
    assert ada in room.contents


@pytest.mark.asyncio
async def test_look_in_player_is_refused():
    engine, room, _coin, _chest, ada, _ben = _market()

    response = await engine.process_command("look in Ada", "Ben")

    assert "no ada" in response.lower()
    assert ada in room.contents


@pytest.mark.asyncio
async def test_put_into_player_is_refused():
    engine, _room, _coin, _chest, ada, ben = _market()
    key = next(item for item in ben.contents if item.name == "Gold Key")

    response = await engine.process_command("put gold key in Ada", "Ben")

    assert "no ada" in response.lower()
    assert key in ben.contents
    assert key not in ada.contents


@pytest.mark.asyncio
async def test_use_key_on_player_is_refused():
    engine, _room, _coin, _chest, _ada, ben = _market()
    key = next(item for item in ben.contents if item.name == "Gold Key")

    response = await engine.process_command("use gold key on Ada", "Ben")

    assert "no ada" in response.lower()
    assert key in ben.contents
