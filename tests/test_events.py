from unittest.mock import AsyncMock

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_item_taken_event_red():
    """[TASK-013] RED PHASE: Verify that taking an item triggers an 'item_taken' event."""
    engine = Engine()
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.")
    
    engine.start_room = room
    player = engine.register_player("Hero")
    room.add_content(sword)
    
    callback = AsyncMock()
    await engine.subscribe("item_taken", callback)
    
    await engine.process_command("take sword", player)
    
    callback.assert_called_once()
    args, _ = callback.call_args
    assert args[0] == "item_taken"
    assert args[1]["item_name"] == "sword"


@pytest.mark.asyncio
async def test_player_said_uses_the_registered_players_room():
    """`say` reports the speaker's current room, not the legacy start room."""
    engine = Engine()
    hall = Room("Hall", "A hall.")
    hall.item_id = "hall"
    kitchen = Room("Kitchen", "A kitchen.")
    kitchen.item_id = "kitchen"
    hall.exits["north"] = kitchen
    engine._rooms["Hall"] = hall
    engine._rooms["Kitchen"] = kitchen
    engine.start_room = hall
    engine.register_player("Ada")

    events = []

    async def listener(event_type, data):
        events.append((event_type, data))

    await engine.subscribe("player_said", listener)
    await engine.process_command("go north", "Ada")
    await engine.process_command("say hello", "Ada")

    assert events[-1][0] == "player_said"
    assert events[-1][1]["location"] == "Kitchen"
