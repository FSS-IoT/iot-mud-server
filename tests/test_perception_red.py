import pytest
from src.mud.engine.core import Engine
from src.mud.content import Entity, Room

@pytest.mark.asyncio
async def test_automatic_room_description_red():
    """[TASK-020] RED PHASE: Verify that moving into a new room automatically provides its description."""
    engine = Engine()
    
    # Setup two rooms
    r1 = Room("Hallway", "A narrow corridor.")
    r2 = Room("Garden", "A beautiful garden full of flowers.")
    r1.exits["north"] = r2
    
    engine._player_location = r1
    engine.player = Entity("Adventurer", "Player")

    # Action: Move North
    response = await engine.process_command("go north")
    
    # Assertions: The response MUST contain the description of the target room (R2)
    assert "A beautiful garden full of flowers" in response, f"Response did not contain the new room's description. Got: '{response}'"
