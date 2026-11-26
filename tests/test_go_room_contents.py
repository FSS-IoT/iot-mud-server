"""Red-green: the `go` reply describes what is in the destination room.

Currently the travel reply only states the description and exits, so walking into
a room never shows its items ("Here you see: Gold Coin."). The reply is the single
source of truth for what a mover sees on arrival, so it must include the room's
contents. A separate `room_state` push on move was removed precisely because it
duplicated this reply.
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_go_reply_includes_destination_items():
    """Moving as a registered player includes the destination's items in the reply."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    market = Room("Marketplace", "Lightly vendors cry out their wares.")
    gate.exits["north"] = market
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    market.add_content(coin)
    engine._rooms = {"Village Gate": gate, "Marketplace": market}
    engine.start_room = gate
    engine.register_player("Ada")

    response = await engine.process_command("go north", "Ada")

    assert "Marketplace" in response
    assert "Gold Coin" in response  # the item is now shown on arrival


@pytest.mark.asyncio
async def test_go_reply_without_items_has_no_contents_line():
    """An empty destination room does not gain a spurious 'Here you see' line."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    hall = Room("Hallway", "A narrow corridor.")
    gate.exits["north"] = hall
    engine._rooms = {"Village Gate": gate, "Hallway": hall}
    engine.start_room = gate
    engine.register_player("Ada")

    response = await engine.process_command("go north", "Ada")

    assert "Hallway" in response
    assert "Here you see:" not in response


@pytest.mark.asyncio
async def test_legacy_go_reply_includes_destination_items():
    """The single actor path also shows room contents on arrival."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    market = Room("Marketplace", "Lightly vendors cry out their wares.")
    gate.exits["north"] = market
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    market.add_content(coin)
    engine._rooms = {"Village Gate": gate, "Marketplace": market}
    engine.start_room = gate
    player = engine.register_player("Adventurer")

    response = await engine.process_command("go north", player)

    assert "Marketplace" in response
    assert "Gold Coin" in response


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
