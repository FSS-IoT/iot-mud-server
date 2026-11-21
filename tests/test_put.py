"""Red-green: `put <item> in <container>`.

`take <loot>` picks things up; `drop <item>` puts them on the floor;
`put <item> in <container>` stores an item inside a container the player is
standing next to. This is the complement to `open`/`take` for containers.

These expect a `put` verb that does not exist yet, so they fail now (RED).
"""

import pytest

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine


async def _setup():
    engine = Engine()
    _player = Player("Ada", "A brave adventurer.")
    room = Room("Market", "A busy marketplace.")
    engine.player = _player
    engine._player_location = room
    return engine, _player, room


@pytest.mark.asyncio
async def test_put_into_open_chest():
    """`put <item> in <chest>` moves the item into the container."""
    engine, player, room = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    chest = Entity("Chest", "A sturdy oak chest.")
    chest.add_content(lantern)
    room.add_content(chest)
    player.add_content(lantern)

    await engine.process_command("put lantern in chest")

    assert lantern in chest.contents
    assert lantern not in player.contents
    assert lantern not in room.contents


@pytest.mark.asyncio
async def test_put_item_whose_name_contains_in():
    """Item names like Gold Coin contain 'in'; split on ' in ', not the substring."""
    engine, player, room = await _setup()
    coin = Entity("Gold Coin", "A gold coin.")
    chest = Entity("Chest", "A sturdy oak chest.")
    room.add_content(chest)
    player.add_content(coin)

    response = await engine.process_command("put gold coin in chest")

    assert response == "You put the Gold Coin in the Chest."
    assert coin in chest.contents
    assert coin not in player.contents


@pytest.mark.asyncio
async def test_put_without_in_returns_hint():
    """A bare `put <item>` asks for the container instead of guessing."""
    engine, player, _room = await _setup()
    coin = Entity("Gold Coin", "A gold coin.")
    player.add_content(coin)

    response = await engine.process_command("put gold coin")

    assert "put what where" in response.lower()
    assert coin in player.contents


@pytest.mark.asyncio
async def test_put_into_alias():
    """`put <item> into <container>` is the same command."""
    engine, player, room = await _setup()
    coin = Entity("Gold Coin", "A gold coin.")
    chest = Entity("Chest", "A sturdy oak chest.")
    room.add_content(chest)
    player.add_content(coin)

    response = await engine.process_command("put gold coin into chest")

    assert response == "You put the Gold Coin in the Chest."
    assert coin in chest.contents


@pytest.mark.asyncio
async def test_put_with_empty_side_returns_hint():
    """`put in chest` has no item; it asks instead of grabbing one."""
    engine, player, room = await _setup()
    chest = Entity("Chest", "A sturdy oak chest.")
    room.add_content(chest)
    coin = Entity("Gold Coin", "A gold coin.")
    player.add_content(coin)

    response = await engine.process_command("put in chest")

    assert "put what where" in response.lower()
    assert coin in player.contents


@pytest.mark.asyncio
async def test_put_missing_item():
    """`put` refuses when the item is not carried."""
    engine, _player, room = await _setup()
    chest = Entity("Chest", "A sturdy oak chest.")
    room.add_content(chest)

    response = await engine.process_command("put lantern in chest")

    assert "carrying" in response.lower()


@pytest.mark.asyncio
async def test_put_missing_container():
    """`put` refuses when there is no container to put it in."""
    engine, player, _room = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    player.add_content(lantern)

    response = await engine.process_command("put lantern in chest")

    assert "chest" in response.lower()


@pytest.mark.asyncio
async def test_put_into_locked_chest():
    """A locked container refuses the item."""
    engine, player, room = await _setup()
    lantern = Entity("Brass Lantern", "A brass lantern.", light_source=True)
    chest = Entity("Chest", "A sturdy oak chest.", locked=True, required_key="key_gold_1")
    room.add_content(chest)
    player.add_content(lantern)

    response = await engine.process_command("put lantern in chest")

    assert "lock" in response.lower()
    assert lantern in player.contents
