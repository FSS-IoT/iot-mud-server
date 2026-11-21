import pytest
from unittest.mock import AsyncMock
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

@pytest.mark.asyncio
async def test_prose_event_on_room_change():
    """[TASK-013] RED PHASE: Verify that changing rooms triggers a 'room_change' event."""
    engine = Engine()
    r1 = Room("Start", "A beginning.")
    r2 = Room("End", "The end.", exits={"back": r1})
    r1.exits["next"] = r2
    
    engine.player = Entity("Hero", "Adventurer.")
    engine._player_location = r1
    
    # Mock subscriber to track calls
    callback = AsyncMock()
    await engine.subscribe("room_change", callback)
    
    # Action: Move room
    await engine.process_command("go next")
    
    # Assertion: Check if event was emitted with expected data
    callback.assert_called_once()
    args, kwargs = callback.call_args
    event_type, data = args
    
    assert event_type == "room_change"
    assert data["from"] == "Start"
    assert data["to"] == "End"
    assert data["desc"] == "The end."
