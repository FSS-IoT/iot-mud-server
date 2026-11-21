import pytest
from unittest.mock import AsyncMock
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

@pytest.mark.asyncio
async def test_item_taken_event_red():
    """[TASK-013] RED PHASE: Verify that taking an item triggers an 'item_taken' event."""
    engine = Engine()
    player = Entity("Hero", "Adventurer.")
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.")
    
    engine.player = player
    room.add_content(sword)
    engine._player_location = room
    
    callback = AsyncMock()
    await engine.subscribe("item_taken", callback)
    
    await engine.process_command("take sword")
    
    callback.assert_called_once()
    args, _ = callback.call_args
    assert args[0] == "item_taken"
    assert args[1]["item_name"] == "sword"
