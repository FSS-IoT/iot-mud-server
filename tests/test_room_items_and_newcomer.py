"""`look` lists the items in a room.

An item such as the Gold Coin must be visible without guessing `take gold
coin`. The newcomer-occupant part of this file was a v1-server test; the v2
room-state path covers it, and the v1 server was retired in #137.
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

# ---------------------------------------------------------------------------
# Bug 1: `look` shows items in the room
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_look_lists_items_in_room():
    """A room holding an item should mention that item when you look."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    gate.add_content(coin)
    engine._rooms["Village Gate"] = gate
    engine.start_room = gate
    player = engine.register_player("Ada")

    response = await engine.process_command("look", player)

    assert "A heavy stone gate stands tall." in response
    # The item is visible without guessing the command.
    assert "Gold Coin" in response


@pytest.mark.asyncio
async def test_look_omits_players_from_item_line():
    """Other players are named on their own line, not lumped into items."""
    engine = Engine()
    room = Room("Market", "Lively vendors cry out their wares.")
    torch = Entity("Torch", "A flickering wooden torch.", weight=0.5)
    room.add_content(torch)
    engine._rooms["Market"] = room
    engine.start_room = room

    # Register two players both standing in the market.
    p1 = engine.register_player("Ada")
    p2 = engine.register_player("Ben")
    p1.location = room
    p2.location = room

    response = await engine.process_command("look", "Ada")

    # Torch is listed as an item...
    assert "Torch" in response
    # ...and Ben is named on the occupants line, not as an item.
    assert "Others in the room: Ben." in response


@pytest.mark.asyncio
async def test_look_without_items_has_no_item_line():
    """A bare room should not invent an items line."""
    engine = Engine()
    room = Room("Empty", "A hollow chamber.")
    engine._rooms["Empty"] = room
    engine.start_room = room
    player = engine.register_player("Ada")

    response = await engine.process_command("look", player)

    assert "A hollow chamber." in response

