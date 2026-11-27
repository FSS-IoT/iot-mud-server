"""Red-green fixes for three demo-friction bugs reported while playing.

Bug 1 (v1 server, retired in #137) was the newcomer room description on join;
the v2 server sends `room.state` on connect and is covered elsewhere.

Bug 2: Registered players cannot take items even in the same room they stand in
(`take gold coin`, `get coin`). `take`/`drop` only used the legacy single-player
slot and matched names exactly, so "coin" never matched "Gold Coin". This ticket
routes take/drop through the registered player's location and makes item-name
matching forgiving.

Bug 3: Players cannot look at another player (`look at Ada`, `examine Ada`). The
`look` verb only ever described the room; there was no way to focus on an entity.
This ticket adds an `examine` verb (and `look at`) that describes a target.

These tests encode behavior that does not exist yet, so they fail now (RED).
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

# ---------------------------------------------------------------------------
# Bug 2: registered players can take items (pure engine)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registered_player_can_take_item():
    """`take gold coin` as a registered player moves the item into their inventory."""
    engine = Engine()
    room = Room("Marketplace", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    room.add_content(coin)
    engine._rooms["Marketplace"] = room
    engine.start_room = room

    player = engine.register_player("Ada")
    assert coin in room.contents  # sanity: it is here before the command

    response = await engine.process_command("take gold coin", "Ada")

    assert coin in player.contents
    assert coin not in room.contents
    assert "picked up" in response.lower()


@pytest.mark.asyncio
async def test_registered_player_take_is_forgiving_on_name():
    """`get coin` matches the item whose display name is 'Gold Coin'."""
    engine = Engine()
    room = Room("Marketplace", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    room.add_content(coin)
    engine._rooms["Marketplace"] = room
    engine.start_room = room

    engine.register_player("Ada")

    response = await engine.process_command("get coin", "Ada")

    assert "picked up" in response.lower()


@pytest.mark.asyncio
async def test_registered_player_can_drop_item():
    """A registered player can drop an item they are holding back into the room."""
    engine = Engine()
    room = Room("Marketplace", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    engine._rooms["Marketplace"] = room
    engine.start_room = room

    player = engine.register_player("Ada")
    player.add_content(coin)  # pretend it is already in their inventory

    response = await engine.process_command("drop gold coin", "Ada")

    assert coin in room.contents
    assert coin not in player.contents
    assert "dropped" in response.lower()


@pytest.mark.asyncio
async def test_legacy_take_still_works_without_player_name():
    """Backward compat: no player_name still uses the legacy slot."""
    engine = Engine()
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.", weight=2.0)
    room.add_content(sword)
    engine.start_room = room
    legacy = engine.register_player("Hero")

    await engine.process_command("take sword", legacy)

    assert sword in legacy.contents
    assert sword not in room.contents


# ---------------------------------------------------------------------------
# Bug 3: examine / look at a target (pure engine)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_examine_player():
    """`exmine Ada` describes the named player."""
    engine = Engine()
    room = Room("Marketplace", "Lightly vendors cry out their wares.")
    engine._rooms["Marketplace"] = room
    engine.start_room = room
    engine.register_player("Ada")
    engine.register_player("Ben")

    response = await engine.process_command("examine Ada", "Ben")

    assert "Ada" in response


@pytest.mark.asyncio
async def test_look_at_player():
    """`look at Ada` is an alias for examining the named player."""
    engine = Engine()
    room = Room("Marketplace", "Lightly vendors cry out their wares.")
    engine._rooms["Marketplace"] = room
    engine.start_room = room
    engine.register_player("Ada")
    engine.register_player("Ben")

    response = await engine.process_command("look at Ada", "Ben")

    assert "Ada" in response


@pytest.mark.asyncio
async def test_examine_item():
    """`examine gold coin` describes the item if the player is holding it."""
    engine = Engine()
    room = Room("Marketplace", "Lightly vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    engine._rooms["Marketplace"] = room
    engine.start_room = room

    player = engine.register_player("Ada")
    player.add_content(coin)

    response = await engine.process_command("examine gold coin", "Ada")

    assert "shiny" in response.lower()  # from the item's description

