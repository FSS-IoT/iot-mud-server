import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_automatic_room_description_red():
    """[TASK-020] RED PHASE: Verify that moving into a new room automatically provides its description."""
    engine = Engine()
    
    # Setup two rooms
    r1 = Room("Hallway", "A narrow corridor.")
    r2 = Room("Garden", "A beautiful garden full of flowers.")
    r1.exits["north"] = r2
    
    engine.start_room = r1
    player = engine.register_player("Adventurer")

    # Action: Move North
    response = await engine.process_command("go north", player)
    
    # Assertions: The response MUST contain the description of the target room (R2)
    assert "A beautiful garden full of flowers" in response, f"Response did not contain the new room's description. Got: '{response}'"
