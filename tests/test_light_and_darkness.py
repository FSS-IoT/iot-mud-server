"""Red-green: light & darkness — properties that affect gameplay.

Rooms and items carry *properties* that change what a player can do, not just
metadata. The first such property is light:

  * A room with a light source (a fixed/portable light in the room) is lit.
  * A player carrying a light source (e.g. a Torch) lights wherever they stand.
  * A dark room with no light obscures vision: `look`, `examine`, and `read`
    all fail because the player cannot see anything.

These tests encode that behavior against an engine that does not yet implement
it, so they fail now (RED).

The default is a LIT room: a room is lit unless it has no light source and the
observer carries none. The demo world has no dark rooms, so every room stays lit
and existing demo tests are unaffected; this file proves the dark path works.
"""

import pytest

from src.mud.content import Entity, Room, Player
from src.mud.engine.core import Engine


def _dark_room_with_torch():
    """A dark room with a Torch on the floor and a player holding nothing."""
    engine = Engine()
    room = Room("Cave", "A pitch-black cavern.", dark=True)
    coin = Entity("Gold Coin", "A shiny, minted copper coin.")
    room.add_content(coin)
    engine._rooms["Cave"] = room
    engine._player_location = room
    return engine, room


def _lit_room_with_torch():
    """A dark room with a Torch on the floor and a player holding the Torch."""
    from src.mud.content import Player
    engine, room = _dark_room_with_torch()
    player = Player("Ada")
    torch = Entity("Torch", "A flickering wooden torch.", light_source=True)
    player.add_content(torch)  # carried items live in the player's contents
    engine._player_location = room
    engine.player = player  # legacy single-player slot holds the player
    return engine, room


@pytest.mark.asyncio
async def test_dark_room_blocks_look():
    """`look` in a dark room reports darkness, not the room's contents."""
    engine, _room = _dark_room_with_torch()

    response = await engine.process_command("look")

    assert "dark" in response.lower()
    assert "Here you see" not in response


@pytest.mark.asyncio
async def test_dark_room_blocks_examine():
    """`examine coin` in a dark room fails — you cannot see it."""
    engine, _room = _dark_room_with_torch()

    response = await engine.process_command("examine coin")

    assert "dark" in response.lower()


@pytest.mark.asyncio
async def test_dark_room_blocks_read():
    """`read sign` in a dark room fails — you cannot see it to read it."""
    engine, room = _dark_room_with_torch()
    sign = Entity("Sign", "A wooden sign.", readable=True)
    room.add_content(sign)

    response = await engine.process_command("read sign")

    assert "dark" in response.lower()


@pytest.mark.asyncio
async def test_carrying_torch_lights_the_room():
    """Carrying a light source lets the player see in a dark room."""
    from src.mud.content import Player
    engine, _room = _dark_room_with_torch()
    player = Player("Ada")
    torch = Entity("Torch", "A flickering wooden torch.", light_source=True)
    player.add_content(torch)  # carried items live in the player's contents
    engine.player = player  # legacy single-player slot holds the player

    response = await engine.process_command("look")

    assert "dark" not in response.lower()
    assert "Here you see" in response
    assert "Gold Coin" in response


@pytest.mark.asyncio
async def test_room_light_source_lights_the_room():
    """A room with its own light source is lit even without a carried light."""
    engine = Engine()
    room = Room("Lantern Room", "A small chamber.", light_source=Entity("Lantern", "A lit lantern."))
    coin = Entity("Gold Coin", "A shiny, minted copper coin.")
    room.add_content(coin)
    engine._rooms["Lantern Room"] = room
    engine._player_location = room

    response = await engine.process_command("look")

    assert "Here you see" in response
    assert "dark" not in response.lower()


@pytest.mark.asyncio
async def test_lit_room_default_sees_items():
    """A room with no explicit darkness stays lit (default-lit world)."""
    engine = Engine()
    room = Room("Market", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.")
    room.add_content(coin)
    engine._rooms["Market"] = room
    engine._player_location = room

    response = await engine.process_command("look")

    assert "Here you see" in response
    assert "Gold Coin" in response


def _make_dark_room_player():
    """A dark room plus a Player standing in it (legacy slot holds the player)."""
    engine, room = _dark_room_with_torch()
    player = Player("Ada")
    player.location = room
    engine._player_location = room
    engine.player = player
    return engine, room


@pytest.mark.asyncio
async def test_dark_room_visible_state_hides_items():
    """The engine's `visible_room_state` hides items in a dark room.

    The server renders room_state by delegating to the engine so visibility
    rules live in one place: a dark room reports its name and darkness but no
    item or occupant names the player cannot see.
    """
    engine, room = _make_dark_room_player()

    view = engine.visible_room_state(engine.player)

    assert view["name"] == room.name
    assert view["dark"] is True
    assert view["items"] == []
    assert view["occupants"] == []


@pytest.mark.asyncio
async def test_lit_room_visible_state_shows_items():
    """A lit room's visible state lists its items and other occupants."""
    engine, room = _make_dark_room_player()
    room.dark = False
    other = Player("Bo")
    other.location = room
    engine._players["Bo"] = other

    view = engine.visible_room_state(engine.player)

    assert view["dark"] is False
    assert "Gold Coin" in view["items"]
    assert "Bo" in view["occupants"]
