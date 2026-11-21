"""Red-green: `take key 1` disambiguation.

When several items share a base name, a bare `take key` is ambiguous. The
`<name> <n>` syntax picks one by 1-based index (in sorted order), e.g.
`take key 1` for the first. These expect a disambiguation path that does not
exist yet, so they fail now (RED).
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
async def test_unique_item_still_resolves_bare_name():
    """A single matching item resolves from a bare name."""
    engine, player, room = await _setup()
    gold = Entity("Gold Key", "A heavy gold key.")
    room.add_content(gold)

    await engine.process_command("take key")

    assert gold in player.contents


@pytest.mark.asyncio
async def test_bare_name_is_ambiguous():
    """A bare name matching several items returns an ambiguity message."""
    engine, player, room = await _setup()
    room.add_content(Entity("Gold Key", "A heavy gold key."))
    room.add_content(Entity("Silver Key", "A slender silver key."))

    response = await engine.process_command("take key")

    assert "several" in response.lower()
    assert "gold key" in response.lower()
    assert "silver key" in response.lower()
    assert not player.contents


@pytest.mark.asyncio
async def test_disambiguate_by_index_first():
    """`key 1` selects the first match in sorted order."""
    engine, player, room = await _setup()
    gold = Entity("Gold Key", "A heavy gold key.")
    silver = Entity("Silver Key", "A slender silver key.")
    room.add_content(gold)
    room.add_content(silver)

    await engine.process_command("take key 1")

    assert gold in player.contents
    assert silver not in player.contents


@pytest.mark.asyncio
async def test_disambiguate_by_index_second():
    """`key 2` selects the second match in sorted order."""
    engine, player, room = await _setup()
    gold = Entity("Gold Key", "A heavy gold key.")
    silver = Entity("Silver Key", "A slender silver key.")
    room.add_content(gold)
    room.add_content(silver)

    await engine.process_command("take key 2")

    assert silver in player.contents
    assert gold not in player.contents


@pytest.mark.asyncio
async def test_disambiguate_bad_index():
    """A missing index resolves to nothing (falls through to the usual refusal)."""
    engine, player, room = await _setup()
    room.add_content(Entity("Gold Key", "A heavy gold key."))

    response = await engine.process_command("take key 3")

    assert "no key" in response.lower()
    assert not player.contents


@pytest.mark.asyncio
async def test_bare_index_does_not_match_every_item():
    """`take 1` has no name; it must not grab the first room item."""
    engine, player, room = await _setup()
    coin = Entity("Gold Coin", "A gold coin.")
    room.add_content(coin)

    response = await engine.process_command("take 1")

    assert "no 1" in response.lower()
    assert coin in room.contents
    assert not player.contents


@pytest.mark.asyncio
async def test_examine_ambiguous():
    """`examine key` on several keys surfaces the ambiguity too."""
    engine, _player, room = await _setup()
    room.add_content(Entity("Gold Key", "A heavy gold key."))
    room.add_content(Entity("Silver Key", "A slender silver key."))

    response = await engine.process_command("examine key")

    assert "several" in response.lower()


@pytest.mark.asyncio
async def test_put_ambiguous_item():
    """`put` refuses when the carried item is ambiguous."""
    engine, player, room = await _setup()
    room.add_content(Entity("Chest", "A sturdy oak chest."))
    player.add_content(Entity("Gold Key", "A heavy gold key."))
    player.add_content(Entity("Silver Key", "A slender silver key."))

    response = await engine.process_command("put key in chest")

    assert "several" in response.lower()
