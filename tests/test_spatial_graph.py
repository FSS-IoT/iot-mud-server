import pytest
import asyncio
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

@pytest.mark.asyncio
async def test_spatial_graph_movement_green():
    """[TASK-010] GREEN PHASE: Verify that 'go north' updates the player to a Room object via the exit link."""
    engine = Engine()
    
    # Setup the physical structure using real objects
    kitchen = Room("Kitchen", "A smelly kitchen.")
    hall = Room("Hall", "A dim hallway.", {"north": kitchen})

    # Inject entities into the engine
    engine._rooms = {
        "Hall": hall,
        "Kitchen": kitchen
    }
    engine._player_location = hall
    
    # Action: Attempt to travel through the graph
    await engine.process_command("go north")
    
    # Assertions: The player should now point to the Kitchen OBJECT, not a string key
    assert engine._player_location is kitchen
    assert engine._player_location.name == "Kitchen"
    assert isinstance(engine._player_location, Room)
