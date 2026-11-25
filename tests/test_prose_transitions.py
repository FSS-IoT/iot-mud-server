from unittest.mock import AsyncMock

import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_prose_event_on_room_change():
    """[TASK-013] RED PHASE: Verify that changing rooms triggers a 'room_change' event."""
    engine = Engine()
    r1 = Room("Start", "A beginning.")
    r2 = Room("End", "The end.", exits={"back": r1})
    r1.exits["next"] = r2
    
    engine.start_room = r1
    player = engine.register_player("Hero")
    
    # Mock subscriber to track calls
    callback = AsyncMock()
    await engine.subscribe("player_moved", callback)
    
    # Action: Move room
    response = await engine.process_command("go next", player)
    
    # Assertion: Check if the move event was emitted with expected data
    callback.assert_called_once()
    args, _kwargs = callback.call_args
    event_type, data = args
    
    assert event_type == "player_moved"
    assert data["name"] == "Hero"
    assert data["from"] == "Start"
    assert data["to"] == "End"
    # The destination prose reaches the actor through the reply.
    assert "The end." in response
