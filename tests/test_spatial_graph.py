
import pytest

from src.mud.content import Room
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
    engine.start_room = hall
    player = engine.register_player("Adventurer")
    
    # Action: Attempt to travel through the graph
    await engine.process_command("go north", player)
    
    # Assertions: The player should now point to the Kitchen OBJECT, not a string key
    assert player.location is kitchen
    assert player.location.name == "Kitchen"
    assert isinstance(player.location, Room)
